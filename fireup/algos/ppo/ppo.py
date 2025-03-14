import time

import gym
import numpy as np
import scipy.signal
import torch
import torch.nn.functional as F

import fireup.algos.ppo.core as core
from fireup.utils.logx import EpochLogger
from fireup.utils.mpi_tools import (
    mpi_avg,
    mpi_fork,
    mpi_statistics_scalar,
    num_procs,
    proc_id,
)
from fireup.utils.mpi_torch import (
    average_gradients,
    setup_pytorch_for_mpi,
    sync_all_params,
)


class PPOBuffer:
    """
    A buffer for storing trajectories experienced by a PPO agent interacting
    with the environment, and using Generalized Advantage Estimation (GAE-Lambda)
    for calculating the advantages of state-action pairs.
    """

    def __init__(self, obs_dim, act_dim, size, gamma=0.99, lam=0.95):
        self.obs_buf = np.zeros(self._combined_shape(size, obs_dim), dtype=np.float32)
        self.act_buf = np.zeros(self._combined_shape(size, act_dim), dtype=np.float32)
        self.adv_buf = np.zeros(size, dtype=np.float32)
        self.rew_buf = np.zeros(size, dtype=np.float32)
        self.ret_buf = np.zeros(size, dtype=np.float32)
        self.val_buf = np.zeros(size, dtype=np.float32)
        self.logp_buf = np.zeros(size, dtype=np.float32)
        self.gamma, self.lam = gamma, lam
        self.ptr, self.path_start_idx, self.max_size = 0, 0, size

    def store(self, obs, act, rew, val, logp):
        """
        Append one timestep of agent-environment interaction to the buffer.
        """
        assert self.ptr < self.max_size  # buffer has to have room so you can store
        self.obs_buf[self.ptr] = obs
        self.act_buf[self.ptr] = act
        self.rew_buf[self.ptr] = rew
        self.val_buf[self.ptr] = val
        self.logp_buf[self.ptr] = logp
        self.ptr += 1

    def finish_path(self, last_val=0):
        """
        Call this at the end of a trajectory, or when one gets cut off
        by an epoch ending. This looks back in the buffer to where the
        trajectory started, and uses rewards and value estimates from
        the whole trajectory to compute advantage estimates with GAE-Lambda,
        as well as compute the rewards-to-go for each state, to use as
        the targets for the value function.

        The "last_val" argument should be 0 if the trajectory ended
        because the agent reached a terminal state (died), and otherwise
        should be V(s_T), the value function estimated for the last state.
        This allows us to bootstrap the reward-to-go calculation to account
        for timesteps beyond the arbitrary episode horizon (or epoch cutoff).
        """

        path_slice = slice(self.path_start_idx, self.ptr)
        rews = np.append(self.rew_buf[path_slice], last_val)
        vals = np.append(self.val_buf[path_slice], last_val)

        # the next two lines implement GAE-Lambda advantage calculation
        deltas = rews[:-1] + self.gamma * vals[1:] - vals[:-1]
        self.adv_buf[path_slice] = self._discount_cumsum(deltas, self.gamma * self.lam)

        # the next line computes rewards-to-go, to be targets for the value function
        self.ret_buf[path_slice] = self._discount_cumsum(rews, self.gamma)[:-1]

        self.path_start_idx = self.ptr

    def get(self):
        """
        Call this at the end of an epoch to get all of the data from
        the buffer, with advantages appropriately normalized (shifted to have
        mean zero and std one). Also, resets some pointers in the buffer.
        """
        assert self.ptr == self.max_size  # buffer has to be full before you can get
        self.ptr, self.path_start_idx = 0, 0
        # the next two lines implement the advantage normalization trick
        adv_mean, adv_std = mpi_statistics_scalar(self.adv_buf)
        self.adv_buf = (self.adv_buf - adv_mean) / adv_std
        return [self.obs_buf, self.act_buf, self.adv_buf, self.ret_buf, self.logp_buf]

    def _combined_shape(self, length, shape=None):
        if shape is None:
            return (length,)
        return (length, shape) if np.isscalar(shape) else (length, *shape)

    def _discount_cumsum(self, x, discount):
        """
        magic from rllab for computing discounted cumulative sums of vectors.

        input:
            vector x,
            [x0,
            x1,
            x2]

        output:
            [x0 + discount * x1 + discount^2 * x2,
            x1 + discount * x2,
            x2]
        """
        return scipy.signal.lfilter([1], [1, float(-discount)], x[::-1], axis=0)[::-1]


"""

Proximal Policy Optimization (by clipping),

with early stopping based on approximate KL

"""


def ppo(
    env_fn,
    actor_critic=core.ActorCritic,
    ac_kwargs=dict(),
    seed=0,
    steps_per_epoch=4000,
    epochs=50,
    gamma=0.99,
    clip_ratio=0.2,
    pi_lr=3e-4,
    vf_lr=1e-3,
    train_pi_iters=80,
    train_v_iters=80,
    lam=0.97,
    max_ep_len=1000,
    target_kl=0.01,
    logger_kwargs=dict(),
    save_freq=10,
):
    """

    Args:
        env_fn : A function which creates a copy of the environment.
            The environment must satisfy the OpenAI Gym API.

        actor_critic: The agent's main model which is composed of
            the policy and value function model, where the policy takes
            some state, ``x`` and action ``a``, and value function takes
            the state ``x``. The model returns a tuple of:

            ===========  ================  ======================================
            Symbol       Shape             Description
            ===========  ================  ======================================
            ``pi``       (batch, act_dim)  | Samples actions from policy given
                                           | states.
            ``logp``     (batch,)          | Gives log probability, according to
                                           | the policy, of taking actions ``a``
                                           | in states ``x``.
            ``logp_pi``  (batch,)          | Gives log probability, according to
                                           | the policy, of the action sampled by
                                           | ``pi``.
            ``v``        (batch,)          | Gives the value estimate for states
                                           | in ``x``. (Critical: make sure
                                           | to flatten this via .squeeze()!)
            ===========  ================  ======================================

        ac_kwargs (dict): Any kwargs appropriate for the actor_critic
            class you provided to PPO.

        seed (int): Seed for random number generators.

        steps_per_epoch (int): Number of steps of interaction (state-action pairs)
            for the agent and the environment in each epoch.

        epochs (int): Number of epochs of interaction (equivalent to
            number of policy updates) to perform.

        gamma (float): Discount factor. (Always between 0 and 1.)

        clip_ratio (float): Hyperparameter for clipping in the policy objective.
            Roughly: how far can the new policy go from the old policy while
            still profiting (improving the objective function)? The new policy
            can still go farther than the clip_ratio says, but it doesn't help
            on the objective anymore. (Usually small, 0.1 to 0.3.)

        pi_lr (float): Learning rate for policy optimizer.

        vf_lr (float): Learning rate for value function optimizer.

        train_pi_iters (int): Maximum number of gradient descent steps to take
            on policy loss per epoch. (Early stopping may cause optimizer
            to take fewer than this.)

        train_v_iters (int): Number of gradient descent steps to take on
            value function per epoch.

        lam (float): Lambda for GAE-Lambda. (Always between 0 and 1,
            close to 1.)

        max_ep_len (int): Maximum length of trajectory / episode / rollout.

        target_kl (float): Roughly what KL divergence we think is appropriate
            between new and old policies after an update. This will get used
            for early stopping. (Usually small, 0.01 or 0.05.)

        logger_kwargs (dict): Keyword args for EpochLogger.

        save_freq (int): How often (in terms of gap between epochs) to save
            the current policy and value function.

    """

    setup_pytorch_for_mpi()

    logger = EpochLogger(**logger_kwargs)
    logger.save_config(locals())

    seed += 10000 * proc_id()
    torch.manual_seed(seed)
    np.random.seed(seed)

    env = env_fn()
    obs_dim = env.observation_space.shape
    act_dim = env.action_space.shape

    # Share information about action space with policy architecture
    ac_kwargs["action_space"] = env.action_space

    # Main model
    actor_critic = actor_critic(in_features=obs_dim[0], **ac_kwargs)

    # Experience buffer
    local_steps_per_epoch = int(steps_per_epoch / num_procs())
    buf = PPOBuffer(obs_dim, act_dim, local_steps_per_epoch, gamma, lam)

    # Count variables
    var_counts = tuple(
        core.count_vars(module)
        for module in [actor_critic.policy, actor_critic.value_function]
    )
    logger.log("\nNumber of parameters: \t pi: %d, \t v: %d\n" % var_counts)

    # Optimizers
    train_pi = torch.optim.Adam(actor_critic.policy.parameters(), lr=pi_lr)
    train_v = torch.optim.Adam(actor_critic.value_function.parameters(), lr=vf_lr)

    # Sync params across processes
    sync_all_params(actor_critic.parameters())

    def update():
        obs, act, adv, ret, logp_old = [torch.Tensor(x) for x in buf.get()]

        # Training policy
        _, logp, _ = actor_critic.policy(obs, act)
        ratio = (logp - logp_old).exp()
        min_adv = torch.where(adv > 0, (1 + clip_ratio) * adv, (1 - clip_ratio) * adv)
        pi_l_old = -(torch.min(ratio * adv, min_adv)).mean()
        ent = (-logp).mean()  # a sample estimate for entropy

        for i in range(train_pi_iters):
            # Output from policy function graph
            _, logp, _ = actor_critic.policy(obs, act)
            # PPO policy objective
            ratio = (logp - logp_old).exp()
            min_adv = torch.where(
                adv > 0, (1 + clip_ratio) * adv, (1 - clip_ratio) * adv
            )
            pi_loss = -(torch.min(ratio * adv, min_adv)).mean()

            # Policy gradient step
            train_pi.zero_grad()
            pi_loss.backward()
            average_gradients(train_pi.param_groups)
            train_pi.step()

            _, logp, _ = actor_critic.policy(obs, act)
            kl = (logp_old - logp).mean()
            kl = mpi_avg(kl.item())
            if kl > 1.5 * target_kl:
                logger.log("Early stopping at step %d due to reaching max kl." % i)
                break
        logger.store(StopIter=i)

        # Training value function
        v = actor_critic.value_function(obs)
        v_l_old = F.mse_loss(v, ret)
        for _ in range(train_v_iters):
            # Output from value function graph
            v = actor_critic.value_function(obs)
            # PPO value function objective
            v_loss = F.mse_loss(v, ret)

            # Value function gradient step
            train_v.zero_grad()
            v_loss.backward()
            average_gradients(train_v.param_groups)
            train_v.step()

        # Log changes from update
        _, logp, _, v = actor_critic(obs, act)
        ratio = (logp - logp_old).exp()
        min_adv = torch.where(adv > 0, (1 + clip_ratio) * adv, (1 - clip_ratio) * adv)
        pi_l_new = -(torch.min(ratio * adv, min_adv)).mean()
        v_l_new = F.mse_loss(v, ret)
        kl = (logp_old - logp).mean()  # a sample estimate for KL-divergence
        clipped = (ratio > (1 + clip_ratio)) | (ratio < (1 - clip_ratio))
        cf = (clipped.float()).mean()
        logger.store(
            LossPi=pi_l_old.item(),
            LossV=v_l_old.item(),
            KL=kl.item(),
            Entropy=ent.item(),
            ClipFrac=cf.item(),
            DeltaLossPi=(pi_l_new - pi_l_old).item(),
            DeltaLossV=(v_l_new - v_l_old).item(),
        )

    start_time = time.time()
    o, r, d, ep_ret, ep_len = env.reset()[0], 0, False, 0, 0

    # Main loop: collect experience in env and update/log each epoch
    for epoch in range(epochs):
        actor_critic.eval()
        for t in range(local_steps_per_epoch):
            a, _, logp_t, v_t = actor_critic(torch.Tensor(o.reshape(1, -1)))

            # save and log
            buf.store(o, a.detach().numpy(), r, v_t.item(), logp_t.detach().numpy())
            logger.store(VVals=v_t.item())

            o, r, d, _, _ = env.step(a.detach().numpy()[0])
            ep_ret += r
            ep_len += 1

            terminal = d or (ep_len == max_ep_len)
            if terminal or (t == local_steps_per_epoch - 1):
                if not (terminal):
                    print("Warning: trajectory cut off by epoch at %d steps." % ep_len)
                # if trajectory didn't reach terminal state, bootstrap value target
                last_val = (
                    r
                    if d
                    else actor_critic.value_function(
                        torch.Tensor(o.reshape(1, -1))
                    ).item()
                )
                buf.finish_path(last_val)
                if terminal:
                    # only save EpRet / EpLen if trajectory finished
                    logger.store(EpRet=ep_ret, EpLen=ep_len)
                o, r, d, ep_ret, ep_len = env.reset()[0], 0, False, 0, 0

        # Save model
        if (epoch % save_freq == 0) or (epoch == epochs - 1):
            logger.save_state({"env": env}, actor_critic, None)

        # Perform PPO update!
        actor_critic.train()
        update()

        # Log info about epoch
        logger.log_tabular("Epoch", epoch)
        logger.log_tabular("EpRet", with_min_and_max=True)
        logger.log_tabular("EpLen", average_only=True)
        logger.log_tabular("VVals", with_min_and_max=True)
        logger.log_tabular("TotalEnvInteracts", (epoch + 1) * steps_per_epoch)
        logger.log_tabular("LossPi", average_only=True)
        logger.log_tabular("LossV", average_only=True)
        logger.log_tabular("DeltaLossPi", average_only=True)
        logger.log_tabular("DeltaLossV", average_only=True)
        logger.log_tabular("Entropy", average_only=True)
        logger.log_tabular("KL", average_only=True)
        logger.log_tabular("ClipFrac", average_only=True)
        logger.log_tabular("StopIter", average_only=True)
        logger.log_tabular("Time", time.time() - start_time)
        logger.dump_tabular()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=str, default="HalfCheetah-v2")
    parser.add_argument("--hid", type=int, default=64)
    parser.add_argument("--l", type=int, default=2)
    parser.add_argument("--gamma", type=float, default=0.99)
    parser.add_argument("--seed", "-s", type=int, default=0)
    parser.add_argument("--cpu", type=int, default=4)
    parser.add_argument("--steps", type=int, default=4000)
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--exp_name", type=str, default="ppo")
    args = parser.parse_args()

    mpi_fork(args.cpu)  # run parallel code with mpi

    from fireup.utils.run_utils import setup_logger_kwargs

    logger_kwargs = setup_logger_kwargs(args.exp_name, args.seed)

    ppo(
        lambda: gym.make(args.env),
        actor_critic=core.ActorCritic,
        ac_kwargs=dict(hidden_sizes=[args.hid] * args.l),
        gamma=args.gamma,
        seed=args.seed,
        steps_per_epoch=args.steps,
        epochs=args.epochs,
        logger_kwargs=logger_kwargs,
    )
