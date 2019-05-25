# -*- coding: utf-8 -*-
"""Run module for SAC on TORCS

- Author: Curt Park
- Contact: curt.park@medipixel.io
"""

import argparse

import gym
import numpy as np
import torch
import torch.optim as optim

from algorithms.common.networks.mlp_lstm import MLP, FlattenMLP, TanhGaussianDistParams
from algorithms.sac.agent import SACAgentLSTM

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

# hyper parameters
hyper_params = {
    "GAMMA": 0.99,
    "TAU": 5e-3,
    "W_ENTROPY": 1e-3,
    "W_MEAN_REG": 0.0,
    "W_STD_REG": 0.0,
    "W_PRE_ACTIVATION_REG": 0.0,
    "LR_ACTOR": 3e-4,
    "LR_VF": 3e-4,
    "LR_QF1": 3e-4,
    "LR_QF2": 3e-4,
    "LR_ENTROPY": 3e-4,
    "POLICY_UPDATE_FREQ": 2,
    "BATCH_SIZE": 32,
    "EPISODE_SIZE": int(1e3),
    "STEP_SIZE": int(16),
    "AUTO_ENTROPY_TUNING": True,
    "WEIGHT_DECAY": 0.0,
    "INITIAL_RANDOM_ACTION": int(1e4),
    "PREFILL_BUFFER_LENGTH": int(1e4),
    "MULTIPLE_LEARN": 1,
    "BRAKE_REGION": int(2e5),
    "BRAKE_DIST_MU": int(1e5),
    "BRAKE_DIST_SIGMA": int(3e4),
    "BRAKE_FACTOR": 1e-1,
}


def run(env: gym.Env, args: argparse.Namespace, state_dim: int, action_dim: int):
    """Run training or test.

    Args:
        env (gym.Env): openAI Gym environment with continuous action space
        args (argparse.Namespace): arguments including training settings
        state_dim (int): dimension of states
        action_dim (int): dimension of actions

    """
    hidden_sizes_actor = [256, 256, 256]
    hidden_sizes_vf = [256, 256, 256]
    hidden_sizes_qf = [256, 256, 256]
    lstm_layer_size = 3

    if args.load_from is not None:
        hyper_params["INITIAL_RANDOM_ACTION"] = 0

    # target entropy
    target_entropy = -np.prod((action_dim,)).item()  # heuristic

    # create actor
    actor = TanhGaussianDistParams(
        input_size=state_dim,
        output_size=action_dim,
        hidden_sizes=hidden_sizes_actor,
        lstm_layer_size=lstm_layer_size,
    ).to(device)

    # create v_critic
    vf = MLP(
        input_size=state_dim,
        output_size=1,
        hidden_sizes=hidden_sizes_vf,
        lstm_layer_size=lstm_layer_size,
    ).to(device)
    vf_target = MLP(
        input_size=state_dim,
        output_size=1,
        hidden_sizes=hidden_sizes_vf,
        lstm_layer_size=lstm_layer_size,
    ).to(device)
    vf_target.load_state_dict(vf.state_dict())

    # create q_critic
    qf_1 = FlattenMLP(
        input_size=state_dim + action_dim,
        output_size=1,
        hidden_sizes=hidden_sizes_qf,
        lstm_layer_size=lstm_layer_size,
    ).to(device)
    qf_2 = FlattenMLP(
        input_size=state_dim + action_dim,
        output_size=1,
        hidden_sizes=hidden_sizes_qf,
        lstm_layer_size=lstm_layer_size,
    ).to(device)

    # create optimizers
    actor_optim = optim.Adam(
        actor.parameters(),
        lr=hyper_params["LR_ACTOR"],
        weight_decay=hyper_params["WEIGHT_DECAY"],
    )
    vf_optim = optim.Adam(
        vf.parameters(),
        lr=hyper_params["LR_VF"],
        weight_decay=hyper_params["WEIGHT_DECAY"],
    )
    qf_1_optim = optim.Adam(
        qf_1.parameters(),
        lr=hyper_params["LR_QF1"],
        weight_decay=hyper_params["WEIGHT_DECAY"],
    )
    qf_2_optim = optim.Adam(
        qf_2.parameters(),
        lr=hyper_params["LR_QF2"],
        weight_decay=hyper_params["WEIGHT_DECAY"],
    )

    # make tuples to create an agent
    models = (actor, vf, vf_target, qf_1, qf_2)
    optims = (actor_optim, vf_optim, qf_1_optim, qf_2_optim)

    # create an agent
    agent = SACAgentLSTM(env, args, hyper_params, models, optims, target_entropy)

    # run
    if args.test:
        agent.test()
    else:
        agent.train()
