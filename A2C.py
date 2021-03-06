import os

import numpy as np
from matplotlib import animation
import matplotlib.pyplot as plt

import gym

import torch 
import torch.nn as nn
from torch.optim import Adam
from torch.distributions import Categorical

from collections import OrderedDict

import wandb

###

class ActorCriticPolicy(nn.Module):
    def __init__(self, 
                 input_dim: int = 1, 
                 hidden_layers: dict = None, 
                 output_dim: int = 1, 
                 continuous_actions: bool = False, 
                 batch_size: int = 32,
                 device: str = 'cpu',
                 ):
        super().__init__()
        self.batch_size = batch_size
        # setting io dimensions
        self.input_dim = input_dim
        self.output_dim = output_dim
        # defining pf and vf hidden layers
        # if hidden_layers is not None:
        #     pf_arch = hidden_layers["policy"]
        #     vf_arch = hidden_layers["value"]
        # else:
        #     pf_arch = [4]
        #     vf_arch = [4]
        # building policy function
        pf_layers = [nn.Linear(input_dim, 64), 
                     nn.Tanh(),
                     nn.Linear(64,32), 
                     nn.Tanh(),
                     nn.Linear(32, output_dim),
                     nn.Softmax(dim=-1),
                     ]
        # if continuous_actions:
        #     pf_layers['af'] = nn.Tanh()
        # else:
        #     pf_layers['af'] = nn.Softmax(dim=-1)
        # building value function
        vf_layers = [nn.Linear(input_dim, 64), 
                     nn.ReLU(), 
                     nn.Linear(64, 32), 
                     nn.ReLU(), 
                     nn.Linear(32,1), 
                     ]
        # defining actor and critic
        self.actor  = nn.ModuleList(pf_layers)
        self.critic = nn.ModuleList(vf_layers)
        # setting device
        self.device = device

    # def compile_layers(self, 
    #                    input_dim: int = 1, 
    #                    output_dim: int = 1, 
    #                    arch: list = [],
    #                    activation: nn.Module = nn.ReLU,
    #                    ):
    #     """
    #         given a list of layers, this builds the full policy (with activation 
    #         functions between layers)
    #     """
    #     net_len = len(arch)
    #     layers = OrderedDict()
    #     prev_size = input_dim
    #     layers['bn'] = nn.InstanceNorm1d(prev_size)
    #     for idx in range(net_len):
    #         layers['af'+str(idx)]  = activation()
    #         layers['lin'+str(idx)] = nn.Linear(prev_size, arch[idx])
    #         prev_size = arch[idx]
    #     layers['out'] = nn.Linear(arch[-1], output_dim)
    #     if activation==nn.Tanh:
    #         layers['out_af'] = nn.Softmax()
    #     return layers

    def reset(self):
        """
            resets the log probability (used for calculating the loss) and
            the reward history (used for computing the discounted reward at 
            each step) of the policy.
        """
        self.log_prob = []
        self.rewards = []

    def forward(self, 
                X: torch.Tensor,
                ):
        """
            completes one forward pass of the network with the observation state x.
        """
        # print("Orig Tensor X: {}".format(X))
        X_act = X
        for layer in self.actor:
            # print("X_act ({}): {}".format(key,X_act))
            X_act = layer(X_act)
        X_cri = X
        for layer in self.critic:
            # print("X_cri ({}): {}".format(key,X_cri))
            X_cri = layer(X_cri)
        # print()
        return X_act, X_cri

    def act(self, 
            obs: torch.Tensor,
            batch_size: int = 1,
            ):
        """
            computes the action distribution and return an action and log probability.
        """
        ######## THIS IS GIVING ISSUES BECAUSE THERE IS NO BATCH #########
        #X = torch.FloatTensor(obs)
        X = obs.view(batch_size,1,-1)
        act_probs, _ = self(X)
        dist = Categorical(act_probs)
        action = dist.sample()
        return action.item()

    def eval(self, 
             obs: torch.Tensor, 
             act: torch.Tensor):
        """
            evaluates an action given a current state, returns log probability, 
            critic's action score and entropy in action distribution
        """
        act_probs, value = self(obs.unsqueeze(dim=1))
        dist = Categorical(act_probs)
        log_probs = dist.log_prob(act).view(-1,1)
        entropy = dist.entropy().mean()
        return value, log_probs, entropy

###

class RolloutBuffer:
    def __init__(self):
        self.actions      = []
        self.observations = []
        self.rewards      = []
        self.cuml_rewards = []
        self.dones        = []

    def reset(self):
        self.actions      = []
        self.observations = []
        self.rewards      = []
        self.cuml_rewards = []
        self.dones        = []

###

class A2C:
    def __init__(self, 
                 env: gym.Env, 
                 policy: nn.Module = None, 
                 policy_kwargs: dict = {}, 
                 actor_optimizer: torch.optim = None, 
                 critic_optimizer: torch.optim = None,
                 hyper_params: dict = {},
                 lr_sched: torch.optim.lr_scheduler = None,
                 batch_size: int = 32,
                 num_epochs: int = 1000,
                 ):
        # Environment
        self.env = env
        self.action_space = env.action_space
        self.observation_space = env.observation_space
        # Hyper parameters
        self._lr           = hyper_params["lr"]           if "lr"           in hyper_params else 3e-3
        self._gamma        = hyper_params["gamma"]        if "gamma"        in hyper_params else 0.99
        self._lambda       = hyper_params["lambda"]       if "lambda"       in hyper_params else 0.95
        self._epsilon      = hyper_params["epsilon"]      if "epsilon"      in hyper_params else 0.2  #check default
        self._entropy_coef = hyper_params["entropy_coef"] if "entropy_coef" in hyper_params else 0.01
        self._critic_coef  = hyper_params["critic_coef"]  if "critic_coef"  in hyper_params else 0.5
        self._clip         = hyper_params["clip"]         if "clip"         in hyper_params else 0.5
        self._batch_size   = hyper_params["batch_size"]   if "batch_size"   in hyper_params else 64
        # Policy
        if type(self.observation_space) is gym.spaces.Discrete:
            input_dim = self.observation_space.n
        else:
            input_dim  = self.observation_space.shape
            if len(input_dim) == 1:
                input_dim = input_dim[0]
        if type(self.action_space) is gym.spaces.Discrete:
            output_dim = self.action_space.n
        else:
            output_dim = self.action_space.shape
            if len(output_dim) == 1:
                output_dim = output_dim[0]
        hidden_layers = policy_kwargs["network_arch"] if ( (policy_kwargs is not None) and ("network_arch" in policy_kwargs) ) else None
        # Define Actor Critic Policy
        if policy is None:
            self.policy = ActorCriticPolicy(input_dim=input_dim, 
                                            hidden_layers=hidden_layers,
                                            output_dim=output_dim, 
                                            batch_size=batch_size,
                                            )
        else:
            self.policy = policy
        self.old_policy = ActorCriticPolicy(input_dim=input_dim, 
                                            hidden_layers=hidden_layers,
                                            output_dim=output_dim, 
                                            batch_size=batch_size,
                                            )
        self.old_policy.load_state_dict(self.policy.state_dict())
        # Optimizer
        self.optimizer  = actor_optimizer  if actor_optimizer  is not None else Adam(self.policy.parameters(),  lr=self._lr, eps=self._epsilon)
        # self.actor_optimizer  = actor_optimizer  if actor_optimizer  is not None else Adam(self.policy.actor.parameters(),  lr=self._lr, eps=self._epsilon)
        # self.critic_optimizer = critic_optimizer if critic_optimizer is not None else Adam(self.policy.critic.parameters(), lr=self._lr, eps=self._epsilon)
        # lr schedule
        if lr_sched:
           self.lr_sched = lr_sched(self.optimizer, max_lr=0.1, epochs=num_epochs, steps_per_epoch=env._max_episode_steps)
        # RolloutBuffer
        self.rollout = RolloutBuffer()

    def learn(self):
        acts = torch.FloatTensor(self.rollout.actions)
        obs  = torch.stack(self.rollout.observations)
        rews = torch.FloatTensor(self.rollout.cuml_rewards)
        # compute advantages
        values, log_probs, entropy = self.policy.eval(obs, acts)
        advantages = rews - values.squeeze() 
        # critic loss
        torch.nn.utils.clip_grad_norm(self.policy.critic.parameters(), self._clip)
        critic_loss = advantages.pow(2).mean()
        # update optimizer
        # self.critic_optimizer.zero_grad()
        # critic_loss.backward()
        # self.critic_optimizer.step()
        # actor loss
        torch.nn.utils.clip_grad_norm_(self.policy.actor.parameters(), self._clip)
        actor_loss = -(log_probs*advantages.detach()).mean()
        # update optimizer
        # self.actor_optimizer.zero_grad()
        # actor_loss.backward()
        # self.actor_optimizer.step()
        # total loss
        loss = (self._critic_coef * critic_loss) + actor_loss - (self._entropy_coef*entropy)
        #
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()
        #
        wandb.log({'learn/advantages':advantages.mean().item(), 
                   'learn/entropy':entropy.item(),
                   'learn/value_loss':critic_loss.item(), 
                   'learn/policy_loss':actor_loss.item(), 
                   'learn/loss':loss.item()
                   }, 
                  commit=True,
                  )
        # print(f'Dones: {self.rollout.dones}')
        # print(f'Observations: {obs.squeeze()}')
        # print(f'Values: {values.squeeze()}')
        # print(f'Advantages: {advantages.squeeze()}')
        # print(f'Value Loss: {critic_loss.squeeze()}')
        # print(f'Policy Loss: {actor_loss.squeeze()}')
        # print()
        return values.mean().item(), critic_loss.item(), actor_loss.item(), loss.item()

    def compute_cumulative_rewards(self):
        cuml_rewards = []
        episode_rewards = []
        rewards = torch.FloatTensor(self.rollout.rewards)
        observations = torch.stack(self.rollout.observations)
        dones = torch.FloatTensor(self.rollout.dones)
        if dones[-1] == True:
            curr_rew = rewards[-1]
        else:
            dummy_var = observations[-1].view(1,1,-1)
            for layer in self.policy.critic:
                dummy_var = layer(dummy_var)
            curr_rew = dummy_var.squeeze().detach()
        cuml_rewards.append(curr_rew)
        for i in reversed(range(0,len(rewards)-1)):
            if dones[i]==True:
                curr_rew = rewards[i]
                episode_rewards.append(curr_rew)
            else:
                curr_rew = rewards[i] + self._gamma*curr_rew
            cuml_rewards.append(curr_rew)
        cuml_rewards.reverse()
        self.rollout.cuml_rewards = cuml_rewards

    def get_action(self, 
                   obs: np.array,
                   ):
        actions = self.policy.act(obs)
        return actions

    def save(self, 
             model_path: str = 'model_ckpts/model_a2c',
             ):
        path = '/'.join(model_path.split('/')[:-1])
        if not os.path.exists(path):
            os.mkdir(path)
        file_name = model_path+'_'+self.env.unwrapped.spec.id
        # torch.save({'model_state_dict': self.policy.state_dict(), 
        #             'optim_state_dict': self.optimizer.state_dict()},
        #            file_name)

    def load(self,
             file_name: str = 'module_ckpts/model_a2c',
             ):
        state_dict = torch.load(file_name)
        self.policy.load_state_dict(state_dict['model_state_dict'])
        self.optim.load_state_dict(state_dict['optim_state_dict'])


###

def do_n_epochs(model: ActorCriticPolicy,
                num_episodes: int = 1e3,
                batch_size: int = 32,
                episode_rews: list = [],
                values: list = [],
                loss: list = [],
                save_model: bool = False,
                save_model_name: str = 'model_ckpts/model_a2c',
                lr_sched: bool = False,
                ):
    curr_step = 0
    episode_rew = 0

    obs = model.env.reset()

    epi_num = 0
    while epi_num < num_episodes:
        # print(f'Episode: {epi}')
        for i in range(batch_size):
            obs = torch.Tensor(obs)
            action = model.policy.act(obs)
            next_obs, rew, done, _ = model.env.step(action)
            episode_rew += rew

            model.rollout.actions.append(action)
            model.rollout.observations.append(obs)
            model.rollout.rewards.append(rew)
            model.rollout.dones.append(done)

            if done:
                obs = model.env.reset()
                episode_rews.append(episode_rew)
                wandb.log({'epi_rew':episode_rew, 'episode':epi_num}, commit=False)
                episode_rew = 0
                epi_num+=1
            else:
                obs = next_obs
            
            info = {'curr_step':curr_step, 
                    'action':action, 
                    'cart_pos':obs[0],
                    'cart_vel':obs[1],
                    'pole_ang':obs[2],
                    'pole_vel':obs[3],
                    }
            wandb.log(info, commit=False)

        model.compute_cumulative_rewards()
        
        curr_step += batch_size

        value, val_loss, pol_loss, tot_loss = model.learn()
        loss.append(tot_loss)
        if curr_step % 100 == 0:
            values.append(value)

        # if epi_num % 100 == 0:
        #     # LOG Actor_loss, episode_rew, entropy, value_loss
        #     print("Episode: {:5d}    Avg Epi Rew: {:6.3f}    Val Loss: {:6.3f}    Pol Loss: {:5.3f}    Avg Val: {:3.3f}".format(epi_num, np.mean(episode_rews[-10:]), val_loss, pol_loss, np.mean(values)))

        model.rollout.reset()

        if lr_sched:
            model.lr_sched.step()
            wandb.log({'lr':model.lr_sched.get_last_lr()[0]}, commit=True)
    
    plot({'episode_rewards': episode_rews, 'values': values, 'loss': loss}, curr_step)

    if save_model:
        model.save(save_model_name)

    del model

###

def plot(data, frame_idx):
    plt.figure(figsize=(20,5))
    if data['episode_rewards']:
        ax = plt.subplot(121)
        ax = plt.gca()
        avg_score = np.mean(data['episode_rewards'][-100:])
        plt.title("Frame: {}   Average Score: {:.2f}".format(frame_idx, avg_score))
        plt.grid()
        plt.plot(data['episode_rewards'])
    if data['values']:
        ax = plt.subplot(122)
        avg_value = np.mean(data['values'][-1000:])
        plt.title("Frame: {}   Average Value: {:.2f}".format(frame_idx, avg_value))
        plt.plot(data['values'])
    plt.show()

def save_frames_as_gif(frames, path='./', filename='gym_animation.gif'):
    #Mess with this to change frame size
    plt.figure(figsize=(frames[0].shape[1] / 72.0, frames[0].shape[0] / 72.0), dpi=72)
    patch = plt.imshow(frames[0])
    plt.axis('off')
    def animate(i):
        patch.set_data(frames[i])
    anim = animation.FuncAnimation(plt.gcf(), animate, frames = len(frames), interval=50)
    anim.save(path + filename, writer='pillow', fps=60)

###

def train(model: ActorCriticPolicy,
          num_episodes: int = 1e3,
          max_timestep: int = None,
          rew_threshold: int = None,
          batch_size: int = 32,
          save_model: bool = False,
          save_model_name: str = 'model_ckpts/model_a2c',
          lr_sched: bool = False,
          ):
    if max_timestep:
        model.env._max_episode_steps = max_timestep
        rew_threshold = rew_threshold if rew_threshold else max_timestep // (10/9)
    do_n_epochs(model,
                num_episodes=num_episodes,
                batch_size=batch_size,
                save_model=save_model,
                save_model_name=save_model_name,
                lr_sched=lr_sched,
                )
    return model

def valid(model: ActorCriticPolicy,
          ):
    frames = []
    obs = model.env.reset()
    with torch.no_grad():
        while True:
            action = model.policy.act(torch.FloatTensor(obs))
            obs, rew, done, _ = model.env.step(action)
            frames.append(model.env.render(mode="rgb_array"))
            if done:
                break
    save_frames_as_gif(frames)

###

def main():
    # env definition
    env_name = 'CartPole-v1'
    env = gym.make(env_name)
    num_episodes = 10000
    # hyper parameters
    lr = 1e-3
    batch_size = 64
    hyperparameters = {
                        "lr":           lr,
                        "gamma":        0.99,
                        "lambda":       0.95,
                        "epsilon":      0.2,
                        "entropy_coef": 0.01,
                        "critic_coef":  0.05,
                        "clip":         0.5,
                        "batch_size":   batch_size 
                      }
    policy_kwargs = { "network_arch": { "policy": [16,16], 
                                        "value":  [8,8] } 
                                        }
    # model creation
    model = A2C(env, 
                policy_kwargs=policy_kwargs, 
                hyper_params=hyperparameters,
                lr_sched=torch.optim.lr_scheduler.OneCycleLR,
                batch_size=batch_size,
                num_epochs=int(num_episodes),
                )
    # log
    wandb.init(entity='elfo',
               project='a2c_training', 
               name='debugging_forgetting_shared_lrsched', 
               config=hyperparameters,
               tags=['critic grad clip, lr=1e-3, actor=16, critic=8']
               )
    # wandb.watch(model.policy,
    #             log='all',
    #             log_freq=1,
    #             log_graph=(True),
    #             )
    # training begin
    print("Training...")
    train(model=model,
          num_episodes=num_episodes,
          batch_size=batch_size,
          save_model=True,
          lr_sched=True,
          )

if __name__ == '__main__':
    main()