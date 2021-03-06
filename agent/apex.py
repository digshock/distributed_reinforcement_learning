from model import apex_value
from optimizer import dqn
from distributed_queue import buffer_queue

import tensorflow as tf
import numpy as np

import utils


class Agent:

    def __init__(self, input_shape, num_action,
                 discount_factor, gradient_clip_norm, reward_clipping,
                 start_learning_rate, end_learning_rate, learning_frame,
                 model_name, learner_name):

        self.input_shape = input_shape
        self.num_action = num_action
        self.discount_factor = discount_factor
        self.gradient_clip_norm = gradient_clip_norm
        self.reward_clipping = reward_clipping
        self.start_learning_rate = start_learning_rate
        self.end_learning_rate = end_learning_rate
        self.learning_frame = learning_frame
        
        with tf.variable_scope(model_name):
            with tf.device('cpu'):

                self.state_ph = tf.placeholder(tf.float32, shape=[None, *self.input_shape])
                self.previous_action_ph = tf.placeholder(tf.int32, shape=[None])
                self.next_state_ph = tf.placeholder(tf.float32, shape=[None, *self.input_shape])
                self.action_ph = tf.placeholder(tf.int32, shape=[None])
                self.reward_ph = tf.placeholder(tf.float32, shape=[None])
                self.done_ph = tf.placeholder(tf.bool, shape=[None])
                self.weight_ph = tf.placeholder(tf.float32, shape=[None])

                if reward_clipping == 'abs_one':
                    self.clipped_r_ph = tf.clip_by_value(self.reward_ph, -1.0, 1.0)
                else:
                    self.clipped_r_ph = self.reward_ph

                self.discounts = tf.to_float(~self.done_ph) * self.discount_factor

                self.main_q_value, self.next_main_q_value, self.target_q_value = apex_value.build_network(
                    current_state=self.state_ph,
                    next_state=self.next_state_ph,
                    previous_action=self.previous_action_ph,
                    action=self.action_ph,
                    num_action=self.num_action,
                    hidden_list=[256, 256])

                # self.main_q_value, self.next_main_q_value, self.target_q_value = apex_value.build_simple_network(
                #     current_state=self.state_ph,
                #     next_state=self.next_state_ph,
                #     previous_action=self.previous_action_ph,
                #     action=self.action_ph,
                #     num_action=self.num_action)

                self.next_action = tf.argmax(self.next_main_q_value, axis=1)
                self.state_action_value = dqn.take_state_action_value(
                    state_value=self.main_q_value, action=self.action_ph, num_action=self.num_action)
                self.next_state_action_value = dqn.take_state_action_value(
                    state_value=self.target_q_value, action=self.next_action, num_action=self.num_action)
                self.target_value = self.next_state_action_value * self.discounts + self.clipped_r_ph

                self.td_error = (self.target_value - self.state_action_value) ** 2
                self.weighted_td_error = self.td_error * self.weight_ph
                self.value_loss = tf.reduce_mean(self.weighted_td_error)

            self.num_env_frames = tf.train.get_or_create_global_step()
            self.learning_rate = tf.train.polynomial_decay(self.start_learning_rate, self.num_env_frames, self.learning_frame, self.end_learning_rate)
            self.optimizer = tf.train.AdamOptimizer(self.learning_rate)
            gradients, variable = zip(*self.optimizer.compute_gradients(self.value_loss))
            gradients, _ = tf.clip_by_global_norm(gradients, self.gradient_clip_norm)
            self.train_op = self.optimizer.apply_gradients(zip(gradients, variable), global_step=self.num_env_frames)

        self.main_target = utils.main_to_target(f'{model_name}/main', f'{model_name}/target')
        self.global_to_session = utils.copy_src_to_dst(learner_name, model_name)
        self.saver = tf.train.Saver()

    def target_to_main(self):
        self.sess.run(self.main_target)

    def parameter_sync(self):
        self.sess.run(self.global_to_session)

    def set_session(self, sess):
        self.sess = sess
        self.sess.run(tf.global_variables_initializer())

    def get_policy_and_action(self, state, previous_action, epsilon):
        state = np.stack(state) / 255
        main_q_value = self.sess.run(
            self.main_q_value,
            feed_dict={
                self.state_ph: [state],
                self.previous_action_ph: [previous_action]})

        main_q_value = main_q_value[0]

        if np.random.rand() > epsilon:
            action = np.argmax(main_q_value, axis=0)
        else:
            action = np.random.choice(self.num_action)

        return action, main_q_value, main_q_value[action]

    def target_main_test(self, state, previous_action):
        state = np.stack(state) / 255
        next_main_q_value, target_q_value = self.sess.run(
            [self.next_main_q_value, self.target_q_value],
            feed_dict={
                self.next_state_ph: [state],
                self.action_ph: [previous_action]})
        print(next_main_q_value)
        print(target_q_value)

    def get_td_error(self, state, next_state, previous_action, action, reward, done):
        state = np.stack(state) / 255
        next_state = np.stack(next_state) / 255

        target_value, state_action_value = self.sess.run(
            [self.target_value, self.state_action_value],
            feed_dict={
                self.state_ph: state,
                self.previous_action_ph: previous_action,
                self.next_state_ph: next_state,
                self.action_ph: action,
                self.reward_ph: reward,
                self.done_ph: done})

        td_error = np.abs(target_value - state_action_value)
        return td_error

    def distributed_train(self, state, next_state, previous_action,
                          action, reward, done, is_weight):
        state = np.stack(state) / 255
        next_state = np.stack(next_state) / 255
        
        loss, target_value, state_action_value, _ = self.sess.run(
            [self.value_loss, self.target_value, self.state_action_value, self.train_op],
            feed_dict={
                self.state_ph: state,
                self.next_state_ph: next_state,
                self.previous_action_ph: previous_action,
                self.action_ph: action,
                self.reward_ph: reward,
                self.done_ph: done,
                self.weight_ph: is_weight})

        td_error = np.abs(target_value - state_action_value)
        return loss, td_error

    def train(self, state, next_state, previous_action, action, reward, done):
        state = np.stack(state) / 255
        next_state = np.stack(next_state) / 255
        loss, _ = self.sess.run(
            [self.value_loss, self.train_op],
            feed_dict={
                self.state_ph: state,
                self.next_state_ph: next_state,
                self.previous_action_ph: previous_action,
                self.action_ph: action,
                self.reward_ph: reward,
                self.done_ph: done,
                self.weight_ph: np.ones_like(reward)})