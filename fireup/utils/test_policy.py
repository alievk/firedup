import os
import os.path as osp
import time

import joblib
import torch
import gym

from fireup import EpochLogger


def load_policy(fpath, itr="last"):

    # handle which epoch to load from
    if itr == "last":
        saves = [
            int(x[10:-3])
            for x in os.listdir(fpath)
            if "torch_save" in x and len(x) > 13
        ]
        itr = "%d" % max(saves) if len(saves) > 0 else ""
    else:
        itr = "%d" % itr

    # load the things!
    model = torch.load(osp.join(fpath, "torch_save" + itr + ".pt"))
    model.eval()

    # get the model's policy
    get_action = model.policy

    # Create environment with render mode
    env = gym.make("LunarLander-v2", render_mode="human")

    return env, get_action


def run_policy(env, get_action, max_ep_len=None, num_episodes=100, render=True):
    logger = EpochLogger()
    o, r, d, ep_ret, ep_len, n = env.reset()[0], 0, False, 0, 0, 0
    while n < num_episodes:
        if render:
            env.render()
            time.sleep(1e-3)

        a = get_action(torch.Tensor(o.reshape(1, -1)))[0]
        a_reshaped = a.detach().numpy().reshape(env.action_space.shape)
        o, r, d, _, _ = env.step(a_reshaped)
        ep_ret += r
        ep_len += 1

        if d or (ep_len == max_ep_len):
            logger.store(EpRet=ep_ret, EpLen=ep_len)
            print("Episode %d \t EpRet %.3f \t EpLen %d" % (n, ep_ret, ep_len))
            o, r, d, ep_ret, ep_len = env.reset()[0], 0, False, 0, 0
            n += 1

    logger.log_tabular("EpRet", with_min_and_max=True)
    logger.log_tabular("EpLen", average_only=True)
    logger.dump_tabular()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("fpath", type=str)
    parser.add_argument("--len", "-l", type=int, default=0)
    parser.add_argument("--episodes", "-n", type=int, default=100)
    parser.add_argument("--norender", "-nr", action="store_true")
    parser.add_argument("--itr", "-i", type=int, default=-1)
    args = parser.parse_args()
    env, get_action = load_policy(args.fpath, args.itr if args.itr >= 0 else "last")
    run_policy(env, get_action, args.len, args.episodes, not (args.norender))
