import json
from datetime import datetime

import matplotlib.pyplot as plt
import numpy as np
import psutil
import sumo_rl
import supersuit as ss
import torch
from array2gif import write_gif
from custom.model import CustomActorCriticPolicy
from custom.utils import load_cfg, smooth_data
from stable_baselines3 import PPO
from stable_baselines3.common.callbacks import (
    CallbackList,
    CheckpointCallback,
    EvalCallback,
)
from stable_baselines3.common.vec_env import VecMonitor

# NOTE: Don't forget to execute this script from 1 directory above experiments/

import os
import warnings
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Union

import gym
import numpy as np

from stable_baselines3.common import base_class  # pytype: disable=pyi-error
from stable_baselines3.common.evaluation import evaluate_policy
from stable_baselines3.common.vec_env import DummyVecEnv, VecEnv, sync_envs_normalization

from stable_baselines3.common.callbacks import (
    CallbackList,
    CheckpointCallback,
    EventCallback,
    BaseCallback,
)

class EvalCallback(EventCallback):
    """
    Callback for evaluating an agent.

    .. warning::

      When using multiple environments, each call to  ``env.step()``
      will effectively correspond to ``n_envs`` steps.
      To account for that, you can use ``eval_freq = max(eval_freq // n_envs, 1)``

    :param eval_env: The environment used for initialization
    :param callback_on_new_best: Callback to trigger
        when there is a new best model according to the ``mean_reward``
    :param n_eval_episodes: The number of episodes to test the agent
    :param eval_freq: Evaluate the agent every ``eval_freq`` call of the callback.
    :param log_path: Path to a folder where the evaluations (``evaluations.npz``)
        will be saved. It will be updated at each evaluation.
    :param best_model_save_path: Path to a folder where the best model
        according to performance on the eval env will be saved.
    :param deterministic: Whether the evaluation should
        use a stochastic or deterministic actions.
    :param render: Whether to render or not the environment during evaluation
    :param verbose:
    :param warn: Passed to ``evaluate_policy`` (warns if ``eval_env`` has not been
        wrapped with a Monitor wrapper)
    """

    def __init__(
        self,
        eval_env: Union[gym.Env, VecEnv],
        callback_on_new_best: Optional[BaseCallback] = None,
        n_eval_episodes: int = 5,
        eval_freq: int = 10000,
        log_path: Optional[str] = None,
        best_model_save_path: Optional[str] = None,
        deterministic: bool = True,
        render: bool = False,
        verbose: int = 1,
        warn: bool = True,
    ):
        super(EvalCallback, self).__init__(callback_on_new_best, verbose=verbose)
        self.n_eval_episodes = n_eval_episodes
        self.eval_freq = eval_freq
        self.best_mean_reward = -np.inf
        self.last_mean_reward = -np.inf
        self.deterministic = deterministic
        self.render = render
        self.warn = warn

        # Convert to VecEnv for consistency
        if not isinstance(eval_env, VecEnv):
            eval_env = DummyVecEnv([lambda: eval_env])

        self.eval_env = eval_env
        self.best_model_save_path = best_model_save_path
        # Logs will be written in ``evaluations.npz``
        if log_path is not None:
            log_path = os.path.join(log_path, "evaluations")
        self.log_path = log_path
        self.evaluations_results = []
        self.evaluations_timesteps = []
        self.evaluations_length = []
        # For computing success rate
        self._is_success_buffer = []
        self.evaluations_successes = []

    def _init_callback(self) -> None:
        # Does not work in some corner cases, where the wrapper is not the same
        if not isinstance(self.training_env, type(self.eval_env)):
            warnings.warn("Training and eval env are not of the same type" f"{self.training_env} != {self.eval_env}")

        # Create folders if needed
        if self.best_model_save_path is not None:
            os.makedirs(self.best_model_save_path, exist_ok=True)
        if self.log_path is not None:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)

    def _log_success_callback(self, locals_: Dict[str, Any], globals_: Dict[str, Any]) -> None:
        """
        Callback passed to the  ``evaluate_policy`` function
        in order to log the success rate (when applicable),
        for instance when using HER.

        :param locals_:
        :param globals_:
        """
        info = locals_["info"]

        if locals_["done"]:
            maybe_is_success = info.get("is_success")
            if maybe_is_success is not None:
                self._is_success_buffer.append(maybe_is_success)

    def _on_step(self) -> bool:

        if self.eval_freq > 0 and self.n_calls % self.eval_freq == 0:
            # Sync training and eval env if there is VecNormalize
            sync_envs_normalization(self.training_env, self.eval_env)

            # Reset success rate buffer
            self._is_success_buffer = []

            episode_rewards, episode_lengths = evaluate_policy(
                self.model,
                self.eval_env,
                n_eval_episodes=self.n_eval_episodes,
                render=self.render,
                deterministic=self.deterministic,
                return_episode_rewards=True,
                warn=self.warn,
                callback=self._log_success_callback,
            )

            if self.log_path is not None:
                self.evaluations_timesteps.append(self.num_timesteps)
                self.evaluations_results.append(episode_rewards)
                self.evaluations_length.append(episode_lengths)

                kwargs = {}
                # Save success log if present
                if len(self._is_success_buffer) > 0:
                    self.evaluations_successes.append(self._is_success_buffer)
                    kwargs = dict(successes=self.evaluations_successes)

                np.savez(
                    self.log_path,
                    timesteps=self.evaluations_timesteps,
                    results=self.evaluations_results,
                    ep_lengths=self.evaluations_length,
                    **kwargs,
                )

            mean_reward, std_reward = np.mean(episode_rewards), np.std(episode_rewards)
            mean_ep_length, std_ep_length = np.mean(episode_lengths), np.std(episode_lengths)
            self.last_mean_reward = mean_reward

            if self.verbose > 0:
                print(f"Eval num_timesteps={self.num_timesteps}, " f"episode_reward={mean_reward:.2f} +/- {std_reward:.2f}")
                print(f"Episode length: {mean_ep_length:.2f} +/- {std_ep_length:.2f}")
            # Add to current Logger
            self.logger.record("eval/mean_reward", float(mean_reward))
            self.logger.record("eval/mean_ep_length", mean_ep_length)

            if len(self._is_success_buffer) > 0:
                success_rate = np.mean(self._is_success_buffer)
                if self.verbose > 0:
                    print(f"Success rate: {100 * success_rate:.2f}%")
                self.logger.record("eval/success_rate", success_rate)

            # Dump log so the evaluation results are printed with the correct timestep
            self.logger.record("time/total timesteps", self.num_timesteps, exclude="tensorboard")
            self.logger.dump(self.num_timesteps)

            if mean_reward > self.best_mean_reward:
                if self.verbose > 0:
                    print("New best mean reward!")
                if self.best_model_save_path is not None:
                    self.model.save(os.path.join(self.best_model_save_path, "best_model"))
                self.best_mean_reward = mean_reward
                # Trigger callback if needed
                if self.callback is not None:
                    return self._on_event()
                
            self.model.save(os.path.join(self.best_model_save_path, "model_" + str(self.num_timesteps) +"_" + str(mean_reward)))

        return True

    def update_child_locals(self, locals_: Dict[str, Any]) -> None:
        """
        Update the references to the local variables.

        :param locals_: the local variables during rollout collection
        """
        if self.callback:
            self.callback.update_locals(locals_)


if __name__ == "__main__":

    sumo_tstep = 7
    n_evaluations = 20
    num_cpus = int(psutil.cpu_count() - 1)
    # You can not use LIBSUMO if using more than one env
    num_envs = int(num_cpus - 4)
    # NOTE: these are actual sumo time steps!!
    # determine when to reset envs!!
    train_timeout = int(1.5e4)
    eval_timeout = int(2.5e3)
    num_episodes = 1000
    # actual number of time steps
    total_timesteps = num_episodes * train_timeout * num_envs
    # eval after every episode
    eval_freq = train_timeout // sumo_tstep
    save_path = "./logs/"

    env = sumo_rl.parallel_env(
        net_file="nets/big-intersection/big-intersection.net.xml",
        route_file="nets/big-intersection/routes.rou.xml",
        out_csv_name="outputs/big-intersection/test",
        use_gui=True,
        num_seconds=train_timeout,
        delta_time=sumo_tstep
    )
    eval_env = sumo_rl.parallel_env(
        net_file="nets/big-intersection/big-intersection.net.xml",
        route_file="nets/big-intersection/routes.rou.xml",
        out_csv_name="outputs/big-intersection/eval",
        use_gui=True,
        num_seconds=eval_timeout,
        delta_time=sumo_tstep
    )
    env = ss.pettingzoo_env_to_vec_env_v0(env)
    env = ss.concat_vec_envs_v0(
        env, num_envs, num_cpus=num_cpus, base_class="stable_baselines3"
    )
    env = VecMonitor(env)
    eval_env = ss.pettingzoo_env_to_vec_env_v0(eval_env)
    eval_env = ss.concat_vec_envs_v0(
        eval_env, 1, num_cpus=1, base_class="stable_baselines3"
    )
    eval_env = VecMonitor(eval_env)


    model = PPO(
        CustomActorCriticPolicy,
        env,
        verbose=3,
        gamma=0.99,
        n_steps=256,
        ent_coef=0.0905168,
        learning_rate=0.0001,
        vf_coef=0.042202,
        max_grad_norm=0.9,
        gae_lambda=0.9,
        n_epochs=5,
        clip_range=0.25,
        batch_size=256,
        tensorboard_log=save_path + "tensorboard",
    )

    eval_callback = EvalCallback(
        eval_env,
        best_model_save_path=save_path + "best_model",
        log_path=save_path + "eval",
        eval_freq=eval_freq,
        deterministic=True,
        render=False,
        n_eval_episodes=1,
    )

    train = True
    if train:
        start_time = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        print(f"Start @ {start_time}")

        model.learn(total_timesteps=total_timesteps, callback=eval_callback)
        end_time = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        print(f"End @ {end_time}")

        # save a learned model
        model.save(save_path + "final_model")
