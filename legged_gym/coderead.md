# coderead
## shape of observation
print code
```
print(f"self.torques shape: {self.torques.shape}")  # 打印形状
print(f"self.torques values: {self.torques}")  # 打印具体值
```
base_ang_vel = num_envs, 3
root_states shape: torch.Size([num_envs, 3]) 
measured_heights shape: torch.Size([num_envs, 121])
self.torques shape: torch.Size([num_envs, 6])

no need debug or print
check the code and cfg define in `pointfoot_rough_config.py` and `pointfoot_fine_config.py`

the train main code in `rsl_rl/rsl_rl/runners/on_policy_runner.py`

visualize the result in `tensorboard`
`tensorboard --logdir=./` 

## code edit
- change `lin_vel_y = [-1.0, 1.0]`
- Dec27 20:52 Modify reward:
  1. `ang_vel_xy` 0.005 ---> 0.2
  2. `collision` 1.0 ---> -1.5
  3. `feet_air_time` 1.0 ---> 2.0