import os
import sys
import copy
import numpy as np
import torch
import yaml
import onnxruntime as ort
from scipy.spatial.transform import Rotation as R
from functools import partial
import limxsdk
import limxsdk.robot.Rate as Rate
import limxsdk.robot.Robot as Robot
import limxsdk.robot.RobotType as RobotType
import limxsdk.datatypes as datatypes
import time
from collections import deque

class PointfootController:
    def __init__(self, model_dir, robot, robot_type):
        # Initialize robot and type information
        self.robot = robot
        self.robot_type = robot_type
        # Load configuration and model file paths based on robot type
        self.config_file = f'{model_dir}/{self.robot_type}/params_lab_mlp.yaml'
        self.model_file = f'{model_dir}/{self.robot_type}/policy/policy.onnx'
        self.encoder_file = f'{model_dir}/{self.robot_type}/policy/encoder.onnx'

        # Load configuration settings from the YAML file
        self.load_config(self.config_file)

        # Load the ONNX model of actor-critic and set up input and output names
        self.policy_session = ort.InferenceSession(self.model_file)
        self.policy_input_names = [self.policy_session.get_inputs()[0].name]
        self.policy_output_names = [self.policy_session.get_outputs()[0].name]
        
        # Load the ONNX model of mlp encoder and set up input and output names
        self.encoder_session = ort.InferenceSession(self.encoder_file)
        self.encoder_input_names = [self.encoder_session.get_inputs()[0].name]
        self.encoder_output_names = [self.encoder_session.get_outputs()[0].name]

        # Prepare robot command structure with default values for mode, q, dq, tau, Kp, Kd
        self.robot_cmd = datatypes.RobotCmd()
        self.robot_cmd.mode = [0. for x in range(0, self.joint_num)]
        self.robot_cmd.q = [0. for x in range(0, self.joint_num)]
        self.robot_cmd.dq = [0. for x in range(0, self.joint_num)]
        self.robot_cmd.tau = [0. for x in range(0, self.joint_num)]
        self.robot_cmd.Kp = [self.control_cfg['stiffness'] for x in range(0, self.joint_num)]
        self.robot_cmd.Kd = [self.control_cfg['damping'] for x in range(0, self.joint_num)]

        # Prepare robot state structure
        self.robot_state = datatypes.RobotState()
        self.robot_state.tau = [0. for x in range(0, self.joint_num)]
        self.robot_state.q = [0. for x in range(0, self.joint_num)]
        self.robot_state.dq = [0. for x in range(0, self.joint_num)]
        self.robot_state_tmp = copy.deepcopy(self.robot_state)

        # Initialize IMU (Inertial Measurement Unit) data structure
        self.imu_data = datatypes.ImuData()
        self.imu_data.quat[0] = 0
        self.imu_data.quat[1] = 0
        self.imu_data.quat[2] = 0
        self.imu_data.quat[3] = 1
        self.imu_data_tmp = copy.deepcopy(self.imu_data)

        # Set up a callback to receive updated robot state data
        self.robot_state_callback_partial = partial(self.robot_state_callback)
        self.robot.subscribeRobotState(self.robot_state_callback_partial)

        # Set up a callback to receive updated IMU data
        self.imu_data_callback_partial = partial(self.imu_data_callback)
        self.robot.subscribeImuData(self.imu_data_callback_partial)

        # Set up a callback to receive updated SensorJoy
        self.sensor_joy_callback_partial = partial(self.sensor_joy_callback)
        self.robot.subscribeSensorJoy(self.sensor_joy_callback_partial)
        
        # Initialize gait phase and gait command
        self.gait_command[0] = 2.0 # Gait frequency range [Hz] [1.5-2.5]
        self.gait_command[1] = 0.5 # Phase offset range [0-1]
        self.gait_command[2] = 0.5 # Contact duration range [0-1]
        
        # add a deque to store the observation history (FIFO)
        self.base_ang_vel_queue = deque(maxlen=self.history_length)
        self.projected_gravity_queue = deque(maxlen=self.history_length)
        self.joint_positions_queue = deque(maxlen=self.history_length)
        self.joint_velocities_queue = deque(maxlen=self.history_length)
        self.actions_queue = deque(maxlen=self.history_length)
        self.scaled_commands_queue = deque(maxlen=self.history_length)
        self.gait_phase_queue = deque(maxlen=self.history_length)
        self.gait_command_queue = deque(maxlen=self.history_length)

    # Load the configuration from a YAML file
    def load_config(self, config_file):
        with open(config_file, 'r') as f:
            config = yaml.safe_load(f)

        # Assign configuration parameters to controller variables
        self.joint_names = config['PointfootCfg']['joint_names']
        self.init_state = config['PointfootCfg']['init_state']['default_joint_angle']
        self.stand_duration = config['PointfootCfg']['stand_mode']['stand_duration']
        self.control_cfg = config['PointfootCfg']['control']
        self.rl_cfg = config['PointfootCfg']['normalization']
        self.obs_scales = config['PointfootCfg']['normalization']['obs_scales']
        self.actions_size = config['PointfootCfg']['size']['actions_size']
        self.history_length = config['PointfootCfg']['size']['observations_history_length']
        self.latent_size = config['PointfootCfg']['size']['latent_size']
        self.observations_size = config['PointfootCfg']['size']['observations_size'] * self.history_length + self.latent_size
        self.imu_orientation_offset = np.array(list(config['PointfootCfg']['imu_orientation_offset'].values()))
        self.user_cmd_cfg = config['PointfootCfg']['user_cmd_scales']
        self.loop_frequency = config['PointfootCfg']['loop_frequency']
        self.decimation = config['PointfootCfg']['control']['decimation']
        
        # Initialize variables for actions, observations, and commands
        self.actions = np.zeros(self.actions_size)
        self.observations = np.zeros(self.observations_size)
        self.last_actions = np.zeros(self.actions_size)
        self.commands = np.zeros(3)  # command to the robot (e.g., velocity, rotation)
        self.scaled_commands = np.zeros(3)
        self.base_lin_vel = np.zeros(3)  # base linear velocity
        self.base_position = np.zeros(3)  # robot base position
        self.loop_count = 0  # loop iteration count
        self.stand_percent = 0  # percentage of time the robot has spent in stand mode
        self.policy_session = None  # ONNX model session for policy inference
        self.encoder_session = None  # ONNX model session for encoder inference
        self.joint_num = len(self.joint_names)  # number of joints
        self.gait_phase = np.zeros(2)
        self.gait_command = np.zeros(3)

        # Initialize joint angles based on the initial configuration
        self.init_joint_angles = np.zeros(len(self.joint_names))
        for i in range(len(self.joint_names)):
            self.init_joint_angles[i] = self.init_state[self.joint_names[i]]
        
        # Set initial mode to "STAND"
        self.mode = "STAND"

    # Main control loop
    def run(self):
        # Initialize default joint angles for standing
        self.default_joint_angles = np.array([0.0] * len(self.joint_names))
        self.stand_percent += 1 / (self.stand_duration * self.loop_frequency)
        self.mode = "STAND"
        self.loop_count = 0

        # Set the loop rate based on the frequency in the configuration
        rate = Rate(self.loop_frequency)
        while True:
            self.update()
            rate.sleep()

    # Handle the stand mode for smoothly transitioning the robot into standing
    def handle_stand_mode(self):
        if self.stand_percent < 1:
            for j in range(len(self.joint_names)):
                # Interpolate between initial and default joint angles during stand mode
                pos_des = self.default_joint_angles[j] * (1 - self.stand_percent) + self.init_state[self.joint_names[j]] * self.stand_percent
                self.set_joint_command_aligned(j, pos_des)
            # Increment the stand percentage over time
            self.stand_percent += 1 / (self.stand_duration * self.loop_frequency)
        else:
            # Switch to walk mode after standing
            self.mode = "WALK"

    def align_robot_state(self, robot_state: datatypes.RobotState):
        aligned_robot_state = copy.deepcopy(robot_state)
        aligned_robot_state.q[1] = robot_state.q[3]
        aligned_robot_state.dq[1] = robot_state.dq[3]
        aligned_robot_state.tau[1] = robot_state.tau[3]
        
        aligned_robot_state.q[2] = robot_state.q[1]
        aligned_robot_state.dq[2] = robot_state.dq[1]
        aligned_robot_state.tau[2] = robot_state.tau[1]
        
        aligned_robot_state.q[3] = robot_state.q[4]
        aligned_robot_state.dq[3] = robot_state.dq[4]
        aligned_robot_state.tau[3] = robot_state.tau[4]
        
        aligned_robot_state.q[4] = robot_state.q[2]
        aligned_robot_state.dq[4] = robot_state.dq[2]
        aligned_robot_state.tau[4] = robot_state.tau[2]
        
        return aligned_robot_state
    
    # Handle the walk mode where the robot moves based on computed actions
    def handle_walk_mode(self):
        # Update the temporary robot state and IMU data
        self.robot_state_tmp = self.align_robot_state(copy.deepcopy(self.robot_state))
        self.imu_data_tmp = copy.deepcopy(self.imu_data)

        # Execute actions every 'decimation' iterations
        if self.loop_count % self.control_cfg['decimation'] == 0:
            self.compute_observation()
            self.compute_actions()
            # Clip the actions within predefined limits
            action_min = -self.rl_cfg['clip_scales']['clip_actions']
            action_max = self.rl_cfg['clip_scales']['clip_actions']
            self.actions = np.clip(self.actions, action_min, action_max)

        # Iterate over the joints and set commands based on actions
        joint_pos = np.array(self.robot_state_tmp.q)
        joint_vel = np.array(self.robot_state_tmp.dq)

        for i in range(len(joint_pos)):
            # Compute the limits for the action based on joint position and velocity
            action_min = (joint_pos[i] - self.init_joint_angles[i] +
                          (self.control_cfg['damping'] * joint_vel[i] - self.control_cfg['user_torque_limit']) /
                          self.control_cfg['stiffness'])
            action_max = (joint_pos[i] - self.init_joint_angles[i] +
                          (self.control_cfg['damping'] * joint_vel[i] + self.control_cfg['user_torque_limit']) /
                          self.control_cfg['stiffness'])

            # Clip action within limits
            self.actions[i] = max(action_min / self.control_cfg['action_scale_pos'],
                                  min(action_max / self.control_cfg['action_scale_pos'], self.actions[i]))

            # Compute the desired joint position and set it
            pos_des = self.actions[i] * self.control_cfg['action_scale_pos'] + self.init_joint_angles[i]
            self.set_joint_command_aligned(i, pos_des)

            # Save the last action for reference
            self.last_actions[i] = self.actions[i]
            
    def compute_gait_phase(self):
        """
        Computes the gait phase based on the current loop count and the gait period.
        """
        # Calculate gait indices
        gait_indices = torch.remainder(
            torch.tensor((self.loop_count / self.loop_frequency) * self.gait_command[0], dtype=torch.float32, device='cpu'), 
            1.0
        )
        # Convert to sin/cos representation
        sin_phase = torch.sin(2 * torch.pi * gait_indices)
        cos_phase = torch.cos(2 * torch.pi * gait_indices)
        
        return torch.stack([sin_phase, cos_phase], dim=0)
    
    def compute_encoder_latent(self, current_obs):
        '''
        Computes the encoder latent vector based on the current observation.
        '''
        # Concatenate observations into a single tensor and convert to float32
        input_tensor = np.concatenate([current_obs], axis=0)
        input_tensor = input_tensor.astype(np.float32).reshape(1,-1)
        
        # Create a dictionary of inputs for the policy session
        inputs = {self.encoder_input_names[0]: input_tensor}
        
        # Run the policy session and get the output
        output = self.encoder_session.run(self.encoder_output_names, inputs)
        
        # Flatten the output and store it as actions
        return np.array(output).flatten()
        
        
    def compute_observation(self):
        '''
        Computes the observation based on the current robot state, IMU data, and commands.
        And stores the observation in the history queue.
        '''
        imu_orientation = np.array(self.imu_data_tmp.quat)
        q_wi = R.from_quat(imu_orientation).as_euler('zyx')  # Quaternion to Euler ZYX conversion
        inverse_rot = R.from_euler('zyx', q_wi).inv().as_matrix()  # Get the inverse rotation matrix

        gravity_vector = np.array([0, 0, -1])  # Gravity in world frame
        projected_gravity = np.dot(inverse_rot, gravity_vector)
        
        base_ang_vel = np.array(self.imu_data_tmp.gyro)
        rot = R.from_euler('zyx', self.imu_orientation_offset).as_matrix()
        base_ang_vel = np.dot(rot, base_ang_vel)
        projected_gravity = np.dot(rot, projected_gravity)

        joint_positions = np.array(self.robot_state_tmp.q)
        joint_velocities = np.array(self.robot_state_tmp.dq)

        actions = np.array(self.last_actions)

        command_scaler = np.diag([
            self.user_cmd_cfg['lin_vel_x'],
            self.user_cmd_cfg['lin_vel_y'],
            self.user_cmd_cfg['ang_vel_yaw']
        ])
        scaled_commands = np.dot(command_scaler, self.commands)

        gait_phase = self.compute_gait_phase()
        gait_command = torch.tensor(self.gait_command, dtype=torch.float32)

        # Fill the history queue with the current observation if it is empty
        while len(self.base_ang_vel_queue) < self.history_length:
            self.base_ang_vel_queue.append(base_ang_vel * self.obs_scales['ang_vel'])
            self.projected_gravity_queue.append(projected_gravity)
            self.joint_positions_queue.append((joint_positions - self.init_joint_angles) * self.obs_scales['dof_pos'])
            self.joint_velocities_queue.append(joint_velocities * self.obs_scales['dof_vel'])
            self.actions_queue.append(actions)
            self.scaled_commands_queue.append(scaled_commands)
            self.gait_phase_queue.append(gait_phase.cpu().numpy())
            self.gait_command_queue.append(gait_command.cpu().numpy())

        # Append the current observation to the history queue
        self.base_ang_vel_queue.append(base_ang_vel * self.obs_scales['ang_vel'])
        self.projected_gravity_queue.append(projected_gravity)
        self.joint_positions_queue.append((joint_positions - self.init_joint_angles) * self.obs_scales['dof_pos'])
        self.joint_velocities_queue.append(joint_velocities * self.obs_scales['dof_vel'])
        self.actions_queue.append(actions)
        self.scaled_commands_queue.append(scaled_commands)
        self.gait_phase_queue.append(gait_phase.cpu().numpy())
        self.gait_command_queue.append(gait_command.cpu().numpy())
        
        history_obs = np.concatenate([
            np.array(self.base_ang_vel_queue).flatten(),
            np.array(self.projected_gravity_queue).flatten(),
            np.array(self.joint_positions_queue).flatten(),
            np.array(self.joint_velocities_queue).flatten(),
            np.array(self.actions_queue).flatten(),
            np.array(self.scaled_commands_queue).flatten(),
            np.array(self.gait_phase_queue).flatten(),
            np.array(self.gait_command_queue).flatten()
        ])
        
        observations = np.clip(
            history_obs,
            -self.rl_cfg['clip_scales']['clip_observations'],
            self.rl_cfg['clip_scales']['clip_observations']
        )
        
        encoder_latent = self.compute_encoder_latent(observations)
        
        self.observations = np.concatenate([history_obs, encoder_latent])
        
    
    def compute_actions(self):
        """
        Computes the actions based on the current observations using the policy session.
        """
        # Concatenate observations into a single tensor and convert to float32
        input_tensor = np.concatenate([self.observations], axis=0)
        input_tensor = input_tensor.astype(np.float32).reshape(1,-1)
        
        # Create a dictionary of inputs for the policy session
        inputs = {self.policy_input_names[0]: input_tensor}
        
        # Run the policy session and get the output
        output = self.policy_session.run(self.policy_output_names, inputs)
        
        # Flatten the output and store it as actions
        self.actions = np.array(output).flatten()
        
    def set_joint_command(self, joint_index, position):
        """
        Sends a command to set a joint to the desired position.
        Replace this method with actual implementation according to your hardware.
        
        Parameters:
        joint_index (int): The index of the joint to command.
        position (float): The desired position of the joint.
        """
        self.robot_cmd.q[joint_index] = position
        
    def set_joint_command_aligned(self, joint_index, position):
        
        if joint_index == 2:
            self.robot_cmd.q[1] = position
        elif joint_index == 4:
            self.robot_cmd.q[2] = position
        elif joint_index == 1:
            self.robot_cmd.q[3] = position
        elif joint_index == 3:
            self.robot_cmd.q[4] = position
        else:
            self.robot_cmd.q[joint_index] = position

    def update(self):
        """
        Updates the robot's state based on the current mode and publishes the robot command.
        """
        if self.mode == "STAND":
            self.handle_stand_mode()
        elif self.mode == "WALK":
            self.handle_walk_mode()
        
        # Increment the loop count
        self.loop_count += 1

        # Publish the robot command
        self.robot.publishRobotCmd(self.robot_cmd)
        
    # Callback function for receiving robot command data
    def robot_state_callback(self, robot_state: datatypes.RobotState):
        """
        Callback function to update the robot state from incoming data.
        
        Parameters:
        robot_state (datatypes.RobotState): The current state of the robot.
        """
        self.robot_state = robot_state

    # Callback function for receiving imu data
    def imu_data_callback(self, imu_data: datatypes.ImuData):
        """
        Callback function to update IMU data from incoming data.
        
        Parameters:
        imu_data (datatypes.ImuData): The IMU data containing stamp, acceleration, gyro, and quaternion.
        """
        self.imu_data.stamp = imu_data.stamp
        self.imu_data.acc = imu_data.acc
        self.imu_data.gyro = imu_data.gyro
        
        # Rotate quaternion values
        self.imu_data.quat[0] = imu_data.quat[1]
        self.imu_data.quat[1] = imu_data.quat[2]
        self.imu_data.quat[2] = imu_data.quat[3]
        self.imu_data.quat[3] = imu_data.quat[0]

    # Callback function for receiving sensor joy data
    def sensor_joy_callback(self, sensor_joy: datatypes.SensorJoy):
        self.commands[0] = sensor_joy.axes[1] * 0.5
        self.commands[1] = sensor_joy.axes[0] * 0.5
        self.commands[2] = sensor_joy.axes[2] * 0.5

if __name__ == '__main__':
    # Get the robot type from the environment variable
    robot_type = os.getenv("ROBOT_TYPE")
    
    # Check if the ROBOT_TYPE environment variable is set, otherwise exit with an error
    if not robot_type:
        print("Error: Please set the ROBOT_TYPE using 'export ROBOT_TYPE=<robot_type>'.")
        sys.exit(1)

    # Create a Robot instance of the specified type
    robot = Robot(RobotType.PointFoot)

    # Default IP address for the robot
    robot_ip = "127.0.0.1"
    
    # Check if command-line argument is provided for robot IP
    if len(sys.argv) > 1:
        robot_ip = sys.argv[1]

    # Initialize the robot with the provided IP address
    if not robot.init(robot_ip):
        sys.exit()
    time.sleep(3)
    # Create and run the PointfootController
    controller = PointfootController(f'{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}/model/pointfoot', robot, robot_type)
    controller.run()
