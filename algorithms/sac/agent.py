# -*- coding: utf-8 -*-
"""SAC agent for episodic tasks in OpenAI Gym.

- Author: Curt Park
- Contact: curt.park@medipixel.io
- Paper: https://arxiv.org/pdf/1801.01290.pdf
         https://arxiv.org/pdf/1812.05905.pdf
"""

import argparse
import os
from typing import Tuple

import gym
import numpy as np
import torch
import torch.nn.functional as F
import torch.optim as optim

from algorithms.common.abstract.agent import Agent, AgentLSTM
from algorithms.common.buffer.replay_buffer import ReplayBuffer, EpisodeBuffer
import algorithms.common.helper_functions as common_utils

from env.torcs_envs import DefaultEnv

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


class SACAgent(Agent):
    """SAC agent interacting with environment.

    Attrtibutes:
        memory (ReplayBuffer): replay memory
        actor (nn.Module): actor model to select actions
        actor_target (nn.Module): target actor model to select actions
        actor_optimizer (Optimizer): optimizer for training actor
        critic_1 (nn.Module): critic model to predict state values
        critic_2 (nn.Module): critic model to predict state values
        critic_target1 (nn.Module): target critic model to predict state values
        critic_target2 (nn.Module): target critic model to predict state values
        critic_optimizer1 (Optimizer): optimizer for training critic_1
        critic_optimizer2 (Optimizer): optimizer for training critic_2
        curr_state (np.ndarray): temporary storage of the current state
        target_entropy (int): desired entropy used for the inequality constraint
        beta (float): beta parameter for prioritized replay buffer
        alpha (torch.Tensor): weight for entropy
        alpha_optimizer (Optimizer): optimizer for alpha
        hyper_params (dict): hyper-parameters
        total_step (int): total step numbers
        episode_step (int): step number of the current episode
        update_step (int): step number of updates
        i_episode (int): current episode number

    """

    def __init__(
        self,
        env: DefaultEnv,
        args: argparse.Namespace,
        hyper_params: dict,
        models: tuple,
        optims: tuple,
        target_entropy: float,
    ):
        """Initialization.

        Args:
            env (gym.Env): openAI Gym environment
            args (argparse.Namespace): arguments including hyperparameters and training settings
            hyper_params (dict): hyper-parameters
            models (tuple): models including actor and critic
            optims (tuple): optimizers for actor and critic
            target_entropy (float): target entropy for the inequality constraint

        """
        Agent.__init__(self, env, args)

        self.actor, self.vf, self.vf_target, self.qf_1, self.qf_2 = models
        self.actor_optimizer, self.vf_optimizer = optims[0:2]
        self.qf_1_optimizer, self.qf_2_optimizer = optims[2:4]
        self.hyper_params = hyper_params
        self.curr_state = np.zeros((1,))
        self.total_step = 0
        self.episode_step = 0
        self.update_step = 0
        self.i_episode = 0

        # automatic entropy tuning
        if self.hyper_params["AUTO_ENTROPY_TUNING"]:
            self.target_entropy = target_entropy
            self.log_alpha = torch.zeros(1, requires_grad=True, device=device)
            self.alpha_optimizer = optim.Adam(
                [self.log_alpha], lr=self.hyper_params["LR_ENTROPY"]
            )

        # load the optimizer and model parameters
        if args.load_from is not None and os.path.exists(args.load_from):
            self.load_params(args.load_from)

        self._initialize()

    # pylint: disable=attribute-defined-outside-init
    def _initialize(self):
        """Initialize non-common things."""
        if not self.args.test:
            # replay memory
            self.memory = ReplayBuffer(
                self.hyper_params["BUFFER_SIZE"], self.hyper_params["BATCH_SIZE"]
            )

            brake_x = np.linspace(
                0,
                self.hyper_params["BRAKE_REGION"],
                self.hyper_params["BRAKE_REGION"],
            )

            self.brakes = np.exp(-np.power(brake_x - self.hyper_params["BRAKE_DIST_MU"], 2.) / (2 * np.power(self.hyper_params["BRAKE_DIST_SIGMA"], 2.)))

    def select_action(self, state: np.ndarray) -> np.ndarray:
        """Select an action from the input space."""
        self.curr_state = state
        state = self._preprocess_state(state)

        # if initial random action should be conducted
        if (
            self.total_step < self.hyper_params["INITIAL_RANDOM_ACTION"]
            and not self.args.test
        ):
            return self.env.action_space.sample()

        if self.args.test and not self.is_discrete:
            _, _, _, selected_action, _ = self.actor(state)
        else:
            selected_action, _, _, _, _ = self.actor(state)

        return selected_action.detach().cpu().numpy()

    # pylint: disable=no-self-use
    def _preprocess_state(self, state: np.ndarray) -> torch.Tensor:
        """Preprocess state so that actor selects an action."""
        state = torch.FloatTensor(state).to(device)
        return state

    def step(self, action: np.ndarray) -> Tuple[np.ndarray, np.float64, bool]:
        """Take an action and return the response of the env."""
        next_state, reward, done, _ = self.env.step(action)

        if not self.args.test:
            # if the last state is not a terminal state, store done as false
            done_bool = (
                False if self.episode_step == self.args.max_episode_steps else done
            )
            transition = (self.curr_state, action, reward, next_state, done_bool)
            self._add_transition_to_memory(transition)

        return next_state, reward, done

    def _add_transition_to_memory(self, transition: Tuple[np.ndarray, ...]):
        """Add 1 step and n step transitions to memory."""
        self.memory.add(*transition)

    def update_model(self) -> Tuple[torch.Tensor, ...]:
        """Train the model after each episode."""
        self.update_step += 1

        experiences = self.memory.sample()
        states, actions, rewards, next_states, dones = experiences
        new_actions, log_prob, pre_tanh_value, mu, std = self.actor(states)

        # train alpha
        if self.hyper_params["AUTO_ENTROPY_TUNING"]:
            alpha_loss = (
                -self.log_alpha * (log_prob + self.target_entropy).detach()
            ).mean()

            self.alpha_optimizer.zero_grad()
            alpha_loss.backward()
            self.alpha_optimizer.step()

            alpha = self.log_alpha.exp()
        else:
            alpha_loss = torch.zeros(1)
            alpha = self.hyper_params["W_ENTROPY"]

        # Q function loss
        masks = 1 - dones
        q_1_pred = self.qf_1(states, actions)
        q_2_pred = self.qf_2(states, actions)
        v_target = self.vf_target(next_states)
        q_target = rewards + self.hyper_params["GAMMA"] * v_target * masks
        qf_1_loss = F.mse_loss(q_1_pred, q_target.detach())
        qf_2_loss = F.mse_loss(q_2_pred, q_target.detach())

        # V function loss
        v_pred = self.vf(states)
        q_pred = torch.min(
            self.qf_1(states, new_actions), self.qf_2(states, new_actions)
        )
        v_target = q_pred - alpha * log_prob
        vf_loss = F.mse_loss(v_pred, v_target.detach())

        # train Q functions
        self.qf_1_optimizer.zero_grad()
        qf_1_loss.backward()
        self.qf_1_optimizer.step()

        self.qf_2_optimizer.zero_grad()
        qf_2_loss.backward()
        self.qf_2_optimizer.step()

        # train V function
        self.vf_optimizer.zero_grad()
        vf_loss.backward()
        self.vf_optimizer.step()

        if self.update_step % self.hyper_params["POLICY_UPDATE_FREQ"] == 0:
            # actor loss
            advantage = q_pred - v_pred.detach()
            actor_loss = (alpha * log_prob - advantage).mean()

            # regularization
            if not self.is_discrete:  # iff the action is continuous
                mean_reg = self.hyper_params["W_MEAN_REG"] * mu.pow(2).mean()
                std_reg = self.hyper_params["W_STD_REG"] * std.pow(2).mean()
                pre_activation_reg = self.hyper_params["W_PRE_ACTIVATION_REG"] * (
                    pre_tanh_value.pow(2).sum(dim=-1).mean()
                )
                actor_reg = mean_reg + std_reg + pre_activation_reg

                # actor loss + regularization
                actor_loss += actor_reg

            # train actor
            self.actor_optimizer.zero_grad()
            actor_loss.backward()
            self.actor_optimizer.step()

            # update target networks
            common_utils.soft_update(self.vf, self.vf_target, self.hyper_params["TAU"])
        else:
            actor_loss = torch.zeros(1)

        return (
            actor_loss.item(),
            qf_1_loss.item(),
            qf_2_loss.item(),
            vf_loss.item(),
            alpha_loss.item(),
        )

    def load_params(self, path: str):
        """Load model and optimizer parameters."""
        if not os.path.exists(path):
            print("[ERROR] the input path does not exist. ->", path)
            return

        params = torch.load(path, map_location=device)
        self.actor.load_state_dict(params["actor"])
        self.qf_1.load_state_dict(params["qf_1"])
        self.qf_2.load_state_dict(params["qf_2"])
        self.vf.load_state_dict(params["vf"])
        self.vf_target.load_state_dict(params["vf_target"])
        self.actor_optimizer.load_state_dict(params["actor_optim"])
        self.qf_1_optimizer.load_state_dict(params["qf_1_optim"])
        self.qf_2_optimizer.load_state_dict(params["qf_2_optim"])
        self.vf_optimizer.load_state_dict(params["vf_optim"])

        if self.hyper_params["AUTO_ENTROPY_TUNING"]:
            self.alpha_optimizer.load_state_dict(params["alpha_optim"])

        print("[INFO] loaded the model and optimizer from", path)

    def save_params(self, n_episode: int):
        """Save model and optimizer parameters."""
        params = {
            "actor": self.actor.state_dict(),
            "qf_1": self.qf_1.state_dict(),
            "qf_2": self.qf_2.state_dict(),
            "vf": self.vf.state_dict(),
            "vf_target": self.vf_target.state_dict(),
            "actor_optim": self.actor_optimizer.state_dict(),
            "qf_1_optim": self.qf_1_optimizer.state_dict(),
            "qf_2_optim": self.qf_2_optimizer.state_dict(),
            "vf_optim": self.vf_optimizer.state_dict(),
        }

        if self.hyper_params["AUTO_ENTROPY_TUNING"]:
            params["alpha_optim"] = self.alpha_optimizer.state_dict()

        Agent.save_params(self, params, n_episode)

    def write_log(self, i: int, loss: np.ndarray, score: float = 0.0, policy_update_freq: int = 1, speed: list = None):
        """Write log about loss and score"""
        total_loss = loss.sum()

        max_speed = 0 if speed is None else (max(speed))
        avg_speed = 0 if speed is None else (sum(speed) / len(speed))

        print(
            "[INFO] episode %d, episode_step %d, total step %d, total score: %d\n"
            "total loss: %.3f actor_loss: %.3f qf_1_loss: %.3f qf_2_loss: %.3f "
            "vf_loss: %.3f alpha_loss: %.3f\n"
            "track name: %s, race position: %d, max speed %.2f, avg speed %.2f\n"
            % (
                i,
                self.episode_step,
                self.total_step,
                score,
                total_loss,
                loss[0] * policy_update_freq,  # actor loss
                loss[1],  # qf_1 loss
                loss[2],  # qf_2 loss
                loss[3],  # vf loss
                loss[4],  # alpha loss
                self.env.track_name,
                self.env.last_obs['racePos'],
                max_speed,
                avg_speed
            )
        )

        if self.args.log:
            with open(self.log_filename, "a") as file:
                file.write(
                    "%d;%d;%d;%d;%.3f;%.3f;%.3f;%.3f;%.3f;%.3f;%s;%d;%.2f;%.2f\n"
                    % (
                        i,
                        self.episode_step,
                        self.total_step,
                        score,
                        total_loss,
                        loss[0] * policy_update_freq,  # actor loss
                        loss[1],  # qf_1 loss
                        loss[2],  # qf_2 loss
                        loss[3],  # vf loss
                        loss[4],  # alpha loss
                        self.env.track_name,
                        self.env.last_obs['racePos'],
                        max_speed,
                        avg_speed
                    )
                )

    # pylint: disable=no-self-use, unnecessary-pass
    def pretrain(self):
        """Pretraining steps."""
        pass

    def train(self):
        """Train the agent."""
        # logger
        if self.args.log:
            with open(self.log_filename, "w") as file:
                file.write(str(self.args) + "\n")
                file.write(str(self.hyper_params) + "\n")

        # pre-training if needed
        self.pretrain()

        for self.i_episode in range(1, self.args.episode_num + 1):
            is_relaunch = (self.i_episode - 1) % self.args.relaunch_period == 0
            state = self.env.reset(relaunch=is_relaunch, render=False, sampletrack=True)

            done = False
            score = 0
            self.episode_step = 0
            loss_episode = list()
            speed = list()

            while not done:
                action = self.select_action(state)

                if "BRAKE_ENABLE" in self.hyper_params and self.hyper_params["BRAKE_ENABLE"]:
                    if "BRAKE_REGION" in self.hyper_params and self.total_step < self.hyper_params["BRAKE_REGION"]:
                        if np.random.random() < self.brakes[self.i_episode] * self.hyper_params["BRAKE_FACTOR"]:
                            action = self.env.try_brake(action)

                next_state, reward, done = self.step(action)
                self.total_step += 1
                self.episode_step += 1

                state = next_state
                score += reward

                speed.append(self.env.last_speed)

                # training
                if len(self.memory) >= self.hyper_params["BATCH_SIZE"] and len(self.memory) >= self.hyper_params["PREFILL_BUFFER"]:
                    for _ in range(self.hyper_params["MULTIPLE_LEARN"]):
                        loss = self.update_model()
                        loss_episode.append(loss)  # for logging

            # logging
            if loss_episode:
                avg_loss = np.vstack(loss_episode).mean(axis=0)
                self.write_log(
                    self.i_episode,
                    avg_loss,
                    score,
                    self.hyper_params["POLICY_UPDATE_FREQ"],
                    speed
                )

            if self.i_episode % self.args.save_period == 0:
                self.save_params(self.i_episode)
            if self.i_episode % self.args.test_period == 0:
                self.interim_test()

        # termination
        self.env.close()
        self.save_params(self.i_episode)
        self.interim_test()


class SACAgentLSTM(AgentLSTM):
    """SAC agent interacting with environment.

    Attrtibutes:
        memory (ReplayBuffer): replay memory
        actor (nn.Module): actor model to select actions
        actor_target (nn.Module): target actor model to select actions
        actor_optimizer (Optimizer): optimizer for training actor
        critic_1 (nn.Module): critic model to predict state values
        critic_2 (nn.Module): critic model to predict state values
        critic_target1 (nn.Module): target critic model to predict state values
        critic_target2 (nn.Module): target critic model to predict state values
        critic_optimizer1 (Optimizer): optimizer for training critic_1
        critic_optimizer2 (Optimizer): optimizer for training critic_2
        curr_state (np.ndarray): temporary storage of the current state
        target_entropy (int): desired entropy used for the inequality constraint
        beta (float): beta parameter for prioritized replay buffer
        alpha (torch.Tensor): weight for entropy
        alpha_optimizer (Optimizer): optimizer for alpha
        hyper_params (dict): hyper-parameters
        total_step (int): total step numbers
        episode_step (int): step number of the current episode
        update_step (int): step number of updates
        i_episode (int): current episode number

    """

    def __init__(
        self,
        env: gym.Env,
        args: argparse.Namespace,
        hyper_params: dict,
        models: tuple,
        optims: tuple,
        target_entropy: float,
    ):
        """Initialization.

        Args:
            env (gym.Env): openAI Gym environment
            args (argparse.Namespace): arguments including hyperparameters and training settings
            hyper_params (dict): hyper-parameters
            models (tuple): models including actor and critic
            optims (tuple): optimizers for actor and critic
            target_entropy (float): target entropy for the inequality constraint

        """
        Agent.__init__(self, env, args)

        self.actor, self.vf, self.vf_target, self.qf_1, self.qf_2 = models
        self.actor_optimizer, self.vf_optimizer = optims[0:2]
        self.qf_1_optimizer, self.qf_2_optimizer = optims[2:4]
        self.hyper_params = hyper_params
        self.curr_state = np.zeros((1,))
        self.total_step = 0
        self.episode_step = 0
        self.update_step = 0
        self.i_episode = 0

        # automatic entropy tuning
        if self.hyper_params["AUTO_ENTROPY_TUNING"]:
            self.target_entropy = target_entropy
            self.log_alpha = torch.zeros(1, requires_grad=True, device=device)
            self.alpha_optimizer = optim.Adam(
                [self.log_alpha], lr=self.hyper_params["LR_ENTROPY"]
            )

        # load the optimizer and model parameters
        if args.load_from is not None and os.path.exists(args.load_from):
            self.load_params(args.load_from)

        self._initialize()

    # pylint: disable=attribute-defined-outside-init
    def _initialize(self):
        """Initialize non-common things."""
        if not self.args.test:
            # replay memory
            self.memory = EpisodeBuffer(
                self.hyper_params["EPISODE_SIZE"],
                self.hyper_params["BATCH_SIZE"],
                self.hyper_params["STEP_SIZE"],
            )

            brake_x = np.linspace(
                0,
                self.hyper_params["BRAKE_REGION"],
                self.hyper_params["BRAKE_REGION"],
            )

            self.brakes = np.exp(-np.power(brake_x - self.hyper_params["BRAKE_DIST_MU"], 2.) / (2 * np.power(self.hyper_params["BRAKE_DIST_SIGMA"], 2.)))

    def select_action(self, state, hx, cx) -> np.ndarray:
        """Select an action from the input space."""
        self.curr_state = state
        state = self._preprocess_state(state)

        # if initial random action should be conducted
        if (
            self.total_step < self.hyper_params["INITIAL_RANDOM_ACTION"]
            and not self.args.test
        ):
            return self.env.action_space.sample(), hx, cx

        if self.args.test and not self.is_discrete:
            _, _, _, selected_action, _, hx, cx = self.actor(state, 1, 1, hx, cx)
        else:
            selected_action, _, _, _, _, hx, cx = self.actor(state, 1, 1, hx, cx)

        selected_action = selected_action.squeeze_(0).squeeze_(0)
        return selected_action.detach().cpu().numpy(), hx, cx

    # pylint: disable=no-self-use
    def _preprocess_state(self, state) -> torch.Tensor:
        """Preprocess state so that actor selects an action."""
        state = torch.FloatTensor(state).to(device)
        return state

    def step(self, action) -> Tuple[np.ndarray, np.float64, bool]:
        """Take an action and return the response of the env."""
        next_state, reward, done, _ = self.env.step(action)

        if not self.args.test:
            # if the last state is not a terminal state, store done as false
            done_bool = (
                False if self.episode_step == self.args.max_episode_steps else done
            )
            transition = (self.curr_state, action, reward, next_state, done_bool)
            self._add_transition_to_memory(transition)

        return next_state, reward, done

    def _add_transition_to_memory(self, transition):
        """Add 1 step and n step transitions to memory."""
        self.memory.add(*transition)

    def update_model(self) -> Tuple[torch.Tensor, ...]:
        """Train the model after each episode."""
        self.update_step += 1

        batch_size, step_size = self.hyper_params["BATCH_SIZE"], self.hyper_params["STEP_SIZE"]

        hx, cx = self.actor.init_lstm_states(batch_size)

        experiences = self.memory.sample()
        states, actions, rewards, next_states, dones = experiences
        new_actions, log_prob, pre_tanh_value, mu, std, _, _ = self.actor(states, batch_size, step_size, hx, cx)

        # train alpha
        if self.hyper_params["AUTO_ENTROPY_TUNING"]:
            alpha_loss = (
                -self.log_alpha * (log_prob + self.target_entropy).detach()
            ).mean()

            self.alpha_optimizer.zero_grad()
            alpha_loss.backward()
            self.alpha_optimizer.step()

            alpha = self.log_alpha.exp()
        else:
            alpha_loss = torch.zeros(1)
            alpha = self.hyper_params["W_ENTROPY"]

        # Q function loss
        masks = 1 - dones
        hx, cx = self.actor.init_lstm_states(batch_size)
        q_1_pred, _, _ = self.qf_1(states, actions, batch_size, step_size, hx, cx)
        hx, cx = self.actor.init_lstm_states(batch_size)
        q_2_pred, _, _ = self.qf_2(states, actions, batch_size, step_size, hx, cx)
        hx, cx = self.actor.init_lstm_states(batch_size)
        v_target, _, _ = self.vf_target(next_states, batch_size, step_size, hx, cx)
        q_target = rewards.view(batch_size, step_size, 1) + self.hyper_params["GAMMA"] * v_target * masks.view(batch_size, step_size, 1)
        qf_1_loss = F.mse_loss(q_1_pred, q_target.detach())
        qf_2_loss = F.mse_loss(q_2_pred, q_target.detach())

        # V function loss
        hx, cx = self.actor.init_lstm_states(batch_size)
        v_pred, _, _ = self.vf(states, batch_size, step_size, hx, cx)
        hx, cx = self.actor.init_lstm_states(batch_size)
        qf_1_pred, _, _ = self.qf_1(states, new_actions, batch_size, step_size, hx, cx)
        hx, cx = self.actor.init_lstm_states(batch_size)
        qf_2_pred, _, _ = self.qf_2(states, new_actions, batch_size, step_size, hx, cx)
        q_pred = torch.min(qf_1_pred, qf_2_pred)

        v_target = q_pred - alpha * log_prob
        vf_loss = F.mse_loss(v_pred, v_target.detach())

        # train Q functions
        self.qf_1_optimizer.zero_grad()
        qf_1_loss.backward()
        self.qf_1_optimizer.step()

        self.qf_2_optimizer.zero_grad()
        qf_2_loss.backward()
        self.qf_2_optimizer.step()

        # train V function
        self.vf_optimizer.zero_grad()
        vf_loss.backward()
        self.vf_optimizer.step()

        if self.update_step % self.hyper_params["POLICY_UPDATE_FREQ"] == 0:
            # actor loss
            advantage = q_pred - v_pred.detach()
            actor_loss = (alpha * log_prob - advantage).mean()

            # regularization
            if not self.is_discrete:  # iff the action is continuous
                mean_reg = self.hyper_params["W_MEAN_REG"] * mu.pow(2).mean()
                std_reg = self.hyper_params["W_STD_REG"] * std.pow(2).mean()
                pre_activation_reg = self.hyper_params["W_PRE_ACTIVATION_REG"] * (
                    pre_tanh_value.pow(2).sum(dim=-1).mean()
                )
                actor_reg = mean_reg + std_reg + pre_activation_reg

                # actor loss + regularization
                actor_loss += actor_reg

            # train actor
            self.actor_optimizer.zero_grad()
            actor_loss.backward()
            self.actor_optimizer.step()

            # update target networks
            common_utils.soft_update(self.vf, self.vf_target, self.hyper_params["TAU"])
        else:
            actor_loss = torch.zeros(1)

        return (
            actor_loss.item(),
            qf_1_loss.item(),
            qf_2_loss.item(),
            vf_loss.item(),
            alpha_loss.item(),
        )

    def load_params(self, path: str):
        """Load model and optimizer parameters."""
        if not os.path.exists(path):
            print("[ERROR] the input path does not exist. ->", path)
            return

        params = torch.load(path, map_location=device)
        self.actor.load_state_dict(params["actor"])
        self.qf_1.load_state_dict(params["qf_1"])
        self.qf_2.load_state_dict(params["qf_2"])
        self.vf.load_state_dict(params["vf"])
        self.vf_target.load_state_dict(params["vf_target"])
        self.actor_optimizer.load_state_dict(params["actor_optim"])
        self.qf_1_optimizer.load_state_dict(params["qf_1_optim"])
        self.qf_2_optimizer.load_state_dict(params["qf_2_optim"])
        self.vf_optimizer.load_state_dict(params["vf_optim"])

        if self.hyper_params["AUTO_ENTROPY_TUNING"]:
            self.alpha_optimizer.load_state_dict(params["alpha_optim"])

        print("[INFO] loaded the model and optimizer from", path)

    def save_params(self, n_episode: int):
        """Save model and optimizer parameters."""
        params = {
            "actor": self.actor.state_dict(),
            "qf_1": self.qf_1.state_dict(),
            "qf_2": self.qf_2.state_dict(),
            "vf": self.vf.state_dict(),
            "vf_target": self.vf_target.state_dict(),
            "actor_optim": self.actor_optimizer.state_dict(),
            "qf_1_optim": self.qf_1_optimizer.state_dict(),
            "qf_2_optim": self.qf_2_optimizer.state_dict(),
            "vf_optim": self.vf_optimizer.state_dict(),
        }

        if self.hyper_params["AUTO_ENTROPY_TUNING"]:
            params["alpha_optim"] = self.alpha_optimizer.state_dict()

        Agent.save_params(self, params, n_episode)

    def write_log(self, i: int, loss: np.ndarray, score: float = 0.0, policy_update_freq: int = 1, speed: list = None):
        """Write log about loss and score"""
        total_loss = loss.sum()

        max_speed = 0 if speed is None else (max(speed))
        avg_speed = 0 if speed is None else (sum(speed) / len(speed))

        print(
            "[INFO] episode %d, episode_step %d, total step %d, total score: %d\n"
            "total loss: %.3f actor_loss: %.3f qf_1_loss: %.3f qf_2_loss: %.3f "
            "vf_loss: %.3f alpha_loss: %.3f\n"
            "track name: %s, race position: %d, max speed %.2f, avg speed %.2f\n"
            % (
                i,
                self.episode_step,
                self.total_step,
                score,
                total_loss,
                loss[0] * policy_update_freq,  # actor loss
                loss[1],  # qf_1 loss
                loss[2],  # qf_2 loss
                loss[3],  # vf loss
                loss[4],  # alpha loss
                self.env.track_name,
                self.env.last_obs['racePos'],
                max_speed,
                avg_speed
            )
        )

        if self.args.log:
            with open(self.log_filename, "a") as file:
                file.write(
                    "%d;%d;%d;%d;%.3f;%.3f;%.3f;%.3f;%.3f;%.3f;%s;%d;%.2f;%.2f\n"
                    % (
                        i,
                        self.episode_step,
                        self.total_step,
                        score,
                        total_loss,
                        loss[0] * policy_update_freq,  # actor loss
                        loss[1],  # qf_1 loss
                        loss[2],  # qf_2 loss
                        loss[3],  # vf loss
                        loss[4],  # alpha loss
                        self.env.track_name,
                        self.env.last_obs['racePos'],
                        max_speed,
                        avg_speed
                    )
                )

    # pylint: disable=no-self-use, unnecessary-pass
    def pretrain(self):
        """Pretraining steps."""
        pass

    def train(self):
        """Train the agent."""
        # logger
        if self.args.log:
            with open(self.log_filename, "w") as file:
                file.write(str(self.args) + "\n")
                file.write(str(self.hyper_params) + "\n")

        # pre-training if needed
        self.pretrain()

        for self.i_episode in range(1, self.args.episode_num + 1):
            is_relaunch = (self.i_episode - 1) % self.args.relaunch_period == 0
            state = self.env.reset(relaunch=is_relaunch, render=False, sampletrack=True)

            hx, cx = self.actor.init_lstm_states(1)

            done = False
            score = 0
            self.episode_step = 0
            loss_episode = list()
            speed = list()

            while not done:
                action, hx, cx = self.select_action(state, hx, cx)

                if "BRAKE_ENABLE" in self.hyper_params and self.hyper_params["BRAKE_ENABLE"]:
                    if "BRAKE_REGION" in self.hyper_params and self.total_step < self.hyper_params["BRAKE_REGION"]:
                        if np.random.random() < self.brakes[self.i_episode] * self.hyper_params["BRAKE_FACTOR"]:
                            action = self.env.try_brake(action)

                next_state, reward, done = self.step(action)
                self.total_step += 1
                self.episode_step += 1

                state = next_state
                score += reward

                speed.append(self.env.last_speed)

                # training
                if len(self.memory) >= self.hyper_params["BATCH_SIZE"] and len(self.memory) >= self.hyper_params["PREFILL_BUFFER"]:
                    for _ in range(self.hyper_params["MULTIPLE_LEARN"]):
                        loss = self.update_model()
                        loss_episode.append(loss)  # for logging

            # logging
            if loss_episode:
                avg_loss = np.vstack(loss_episode).mean(axis=0)
                self.write_log(
                    self.i_episode,
                    avg_loss,
                    score,
                    self.hyper_params["POLICY_UPDATE_FREQ"],
                    speed
                )

            if self.i_episode % self.args.save_period == 0:
                self.save_params(self.i_episode)
            if self.i_episode % self.args.test_period == 0:
                self.interim_test()

        # termination
        self.env.close()
        self.save_params(self.i_episode)
        self.interim_test()
