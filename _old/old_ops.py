#import logging
# logging.basicConfig(format="[%(asctime)s] %(message)s", datefmt="%m-%d %H:%M:%S")

import tensorflow as tf
import numpy as np
from utils import get_shape
from tensorflow.python.ops import rnn_cell
WEIGHT_INITIALIZER = tf.contrib.layers.xavier_initializer()

def conv1d(input, num_outputs, kernel_size, scope='conv1d'):
    with tf.variable_scope(scope):
        batch_size, image_height, image_width, num_channels = get_shape(input)
        kernel_height, kernel_width = kernel_size, 1
        # initialize kernel weights
        weights_shape = [kernel_height, kernel_width, num_channels, num_outputs]
        weights = tf.get_variable("weights", weights_shape, tf.float32, WEIGHT_INITIALIZER, None)

    stride_shape = [1, 1, 1, 1]
    outputs = tf.nn.conv2d(input, weights, stride_shape, padding='SAME', name='conv1d_outputs')
    return outputs

def conv2d(input, num_outputs, kernel_height, kernel_width, mask_type='A', scope='conv2d'):
    with tf.variable_scope(scope):
        batch_size, image_height, image_width, num_channels = get_shape(input)

        center_height = kernel_height // 2
        center_width = kernel_width // 2

        # initialize kernel weights
        weights_shape = [kernel_height, kernel_width, num_channels, num_outputs]
        weights = tf.get_variable("weights", weights_shape, tf.float32, WEIGHT_INITIALIZER, None)

        # pre-convolution mask
        mask_shape = (kernel_height, kernel_width, num_channels, num_outputs)
        mask = np.ones(mask_shape, dtype=np.float32)

        mask[center_height, center_width+1:, :, :] = 0.0
        mask[center_height+1:, :, :, :] = 0.0

        # in type A, we do not allow a connection to the current focus of the kernel
        # which is its center pixel
        if mask_type.lower() == 'a':
            mask[center_height, center_width, :, :] = 0.0

        # apply the mask
        weights *= tf.constant(mask, dtype=tf.float32)
        # store the weights variable
        tf.add_to_collection('conv2d_weights_mask_%s' % mask_type, weights)

    stride_shape = [1, 1, 1, 1]
    outputs = tf.nn.conv2d(input, weights, stride_shape, padding='SAME', name='conv2d_outputs')
    tf.add_to_collection('conv2d_outputs', outputs)
    return outputs

def skew(inputs, scope="skew"):
    with tf.name_scope(scope):
        batch, height, width, channel = get_shape(inputs)
        new_width = width + height - 1
        skewed_rows = []# inputs = tf.zeros([batch, width * 2 - 1 , height, channel])
        #rows = tf.unpack(tf.transpose(inputs, [1, 0, 3, 2])) # [height, batch, channel, width]
        rows = tf.split(1, height, inputs)  # [batch, 1, width, channel]

        for i, row in enumerate(rows):
            transposed_row = tf.transpose(tf.squeeze(row, [1]), [0, 2, 1])
            reshaped_row = tf.reshape(transposed_row, [-1, width]) # [batch * channel, width]
            padded_row = tf.pad(reshaped_row, ((0, 0), (i, height - 1 - i)))

            unsqueezed_row = tf.reshape(padded_row, [-1, channel, new_width])  # [batch, channel, width*2-1]
            new_row = tf.transpose(unsqueezed_row, [0, 2, 1])  # [batch, width*2-1, channel]

            assert get_shape(new_row) == [batch, new_width, channel], "wrong shape of skewed row"
            skewed_rows.append(new_row)

        skewed_inputs = tf.pack(skewed_rows, axis=1, name="skewed_inputs")
        desired_shape = [None, height, new_width, channel]
        skewed_shape = get_shape(skewed_inputs)
        assert skewed_shape == desired_shape, "wrong shape of skewed input. Actual {}; Expected {}".format(skewed_shape, desired_shape)

    return skewed_inputs

def unskew(skewed_outputs, width=0, scope="unskew"):
    with tf.name_scope(scope):
        batch, height, skewed_width, channel = get_shape(skewed_outputs)
        #rows = tf.unpack(tf.transpose(skewed_outputs, [1, 0, 2, 3,]))  # [height, batch, width, channel]
        rows = tf.split(1, height, skewed_outputs)  # [batch, 1, width, channel]
        width = width if width else height

        unskewed_rows = []
        # iterate through the rows
        for i, row in enumerate(rows):
            sliced_row = tf.slice(row, [0, 0, i, 0], [-1, -1, width, -1])
            unskewed_rows.append(sliced_row)
        unskewed_output = tf.concat(1, unskewed_rows, name="unskewed_output")

        desired_shape = [None, height, width, channel]
        output_shape = get_shape(unskewed_output)
        assert output_shape == desired_shape, "wrong shape of unskewed output. Actual {}; Expected {}".format(output_shape, desired_shape)
    return unskewed_output


class DiagonalLSTMCell(rnn_cell.RNNCell):
  def __init__(self, hidden_dims, height, channel):
    self._num_unit_shards = 1
    self._forget_bias = 1.

    self._height = height
    self._channel = channel

    self._hidden_dims = hidden_dims
    self._num_units = self._hidden_dims * self._height
    self._state_size = self._num_units * 2
    self._output_size = self._num_units

  @property
  def state_size(self):
    return self._state_size

  @property
  def output_size(self):
    return self._output_size

  def __call__(self, i_to_s, state, scope="DiagonalBiLSTMCell"):
    c_prev = tf.slice(state, [0, 0], [-1, self._num_units])
    h_prev = tf.slice(state, [0, self._num_units], [-1, self._num_units]) # [batch, height * hidden_dims]

    # i_to_s : [batch, 4 * height * hidden_dims]
    input_size = i_to_s.get_shape().with_rank(2)[1]

    if input_size.value is None:
      raise ValueError("Could not infer input size from inputs.get_shape()[-1]")

    with tf.variable_scope(scope):
      # input-to-state (K_ss * h_{i-1}) : 2x1 convolution. generate 4h x n x n tensor.
      conv1d_inputs = tf.reshape(h_prev,
          [-1, self._height, 1, self._hidden_dims], name='conv1d_inputs') # [batch, height, 1, hidden_dims]

      tf.add_to_collection('i_to_s', i_to_s)
      tf.add_to_collection('conv1d_inputs', conv1d_inputs)

      conv_s_to_s = conv1d(conv1d_inputs,
          4 * self._hidden_dims, 2, scope='s_to_s') # [batch, height, 1, hidden_dims * 4]
      s_to_s = tf.reshape(conv_s_to_s,
          [-1, self._height * self._hidden_dims * 4]) # [batch, height * hidden_dims * 4]

      tf.add_to_collection('conv_s_to_s', conv_s_to_s)
      tf.add_to_collection('s_to_s', s_to_s)

      lstm_matrix = tf.sigmoid(s_to_s + i_to_s)

      # i = input_gate, g = new_input, f = forget_gate, o = output_gate
      i, g, f, o = tf.split(1, 4, lstm_matrix)

      c = f * c_prev + i * g
      h = tf.mul(o, tf.tanh(c), name='hid')


    new_state = tf.concat(1, [c, h])
    return h, new_state

def diagonal_lstm(inputs, hidden_dims, scope='diagonal_lstm'):
    with tf.variable_scope(scope):
        tf.add_to_collection('lstm_inputs', inputs)

        skewed_inputs = skew(inputs, scope="skewed_i")
        tf.add_to_collection('skewed_lstm_inputs', skewed_inputs)

        # input-to-state (K_is * x_i) : 1x1 convolution. generate 4h x n x n tensor.
        input_to_state = conv2d(skewed_inputs, hidden_dims * 4, 1, 1, mask_type="B", scope="i_to_s")
        column_wise_inputs = tf.transpose(
            input_to_state, [0, 2, 1, 3]) # [batch, width, height, hidden_dims * 4]

        tf.add_to_collection('skewed_conv_inputs', input_to_state)
        tf.add_to_collection('column_wise_inputs', column_wise_inputs)

        batch, width, height, channel = get_shape(column_wise_inputs)
        rnn_inputs = tf.reshape(column_wise_inputs,
            [-1, width, height * channel]) # [batch, max_time, height * hidden_dims * 4]

        tf.add_to_collection('rnn_inputs', rnn_inputs)

        rnn_input_list = [tf.squeeze(rnn_input, squeeze_dims=[1])
            for rnn_input in tf.split(split_dim=1, num_split=width, value=rnn_inputs)]

        cell = DiagonalLSTMCell(hidden_dims, height, channel)

        output_list, state_list = tf.nn.rnn(cell,
          inputs=rnn_input_list, dtype=tf.float32) # width * [batch, height * hidden_dims]

        packed_outputs = tf.pack(output_list, 1) # [batch, width, height * hidden_dims]
        width_first_outputs = tf.reshape(packed_outputs,
          [-1, width, height, hidden_dims]) # [batch, width, height, hidden_dims]

        skewed_outputs = tf.transpose(width_first_outputs, [0, 2, 1, 3]) # [batch, height, width, hidden_dims]
        tf.add_to_collection('skewed_outputs', skewed_outputs)

        outputs = unskew(skewed_outputs)
        tf.add_to_collection('unskewed_outputs', outputs)

        return outputs
def diagonal_bilstm(inputs, hidden_dims, use_residual=False, scope='diagonal_bilstm'):
    with tf.variable_scope(scope):
        def reverse(inputs):
          return tf.reverse(inputs, [False, False, True, False])

        output_state_fw = diagonal_lstm(inputs, hidden_dims, scope='output_state_fw')
        output_state_bw = reverse(diagonal_lstm(reverse(inputs), hidden_dims, scope='output_state_bw'))


        if use_residual:
            #conv2d(input, num_outputs, kernel_height, kernel_width, mask_type='A', scope='conv2d'):
            residual_state_fw = conv2d(output_state_fw, hidden_dims * 2, 1, 1, "B", scope="residual_fw")
            output_state_fw = residual_state_fw + inputs

            residual_state_bw = conv2d(output_state_bw, hidden_dims * 2, 1, 1, "B", scope="residual_bw")
            output_state_bw = residual_state_bw + inputs

        batch, height, width, channel = get_shape(output_state_bw)

        output_state_bw_except_last = tf.slice(output_state_bw, [0, 0, 0, 0], [-1, height-1, -1, -1])
        output_state_bw_only_last = tf.slice(output_state_bw, [0, height-1, 0, 0], [-1, 1, -1, -1])
        dummy_zeros = tf.zeros_like(output_state_bw_only_last)

        output_state_bw_with_last_zeros = tf.concat(1, [output_state_bw_except_last, dummy_zeros])

        return output_state_fw + output_state_bw_with_last_zeros
