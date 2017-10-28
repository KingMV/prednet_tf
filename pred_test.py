import numpy as np
import tensorflow as tf
from op import *
import os

class PredNet(object):
    def __init__(self, batch_size, T, layer_loss_weights=np.array([1., 0, 0, 0], np.float32) ,image_size=(128, 160), stack_sizes=(3, 48, 96, 192), R_stack_sizes=(3, 48, 96, 192), A_filter_sizes=(3,3,3), Ahat_filter_sizes=(3,3,3,3), R_filter_sizes=(3, 3, 3, 3), pixel_max=1, output_mode='all', extrap_start_time=None, c_dim=3):
        self.layer_loss_weights = layer_loss_weights
        self.image_size = image_size
        self.T = T
        self.batch_size = batch_size
        self.stack_sizes = stack_sizes
        self.nb_layers = len(stack_sizes)
        self.R_stack_sizes = R_stack_sizes
        self.A_filter_sizes = A_filter_sizes
        self.Ahat_filter_sizes = Ahat_filter_sizes
        self.R_filter_sizes = R_filter_sizes
        self.c_dim = c_dim

        self.pixel_max = pixel_max
        
        default_output_mode = ['predition', 'error', 'all']
        layer_output_mode = [layer + str(n) for n in range(self.nb_layers) for layer in ['R', 'E', 'A', 'Ahat']]
        self.output_mode = output_mode
        if self.output_mode in layer_output_mode:
            self.output_layer_type = self.output_mode[:-1]
            self.output_layer_num = int(self.output_mode[-1])
        else:
            self.output_layer_type = None
            self.output_layer_num = None

        self.input_shape = [batch_size, T, image_size[0], image_size[1], self.c_dim]
        self.build_model()

    def build_model(self):
        self.inputs = tf.placeholder(tf.float32, self.input_shape)
        frame_predictions, errors = self.forward(self.inputs)
        nt = self.T

        errors = tf.concat(axis=1, values=errors)#[b, t, nb_layers]
        self.frame_predictions = tf.concat(axis=1, values=frame_predictions)#[b,t,h,w,c]

    def forward(self, inputs):
        "inputs of size [batch_size, t, h, w, c]"
        states = self.get_initial_state()
        errors = []
        frame_predictions = []
        t = inputs.get_shape().as_list()[1]
        reuse_step = False
        for i in range(t):
            a = inputs[:, i]
            output, states = self.step(a, states, reuse_step=reuse_step)
            frame_predictions.append(tf.reshape(output[0], [self.batch_size, 1, self.image_size[0], self.image_size[1], self.c_dim]))  
            errors.append(tf.reshape(output[1], [self.batch_size, 1, self.nb_layers]))
            reuse_step = True
        return frame_predictions, errors

    def get_initial_state(self):
        #base_initial_state = tf.zeros_like(x) #[b, t, h, w, c]
        #base_initial_state = tf.reduce_sum(base_initial_state, axis=[1, 2, 3])
        batch = self.batch_size
        init_nb_row = self.image_size[0]
        init_nb_col = self.image_size[1]
        initial_states = []
        states_to_pass = ['r', 'c', 'e']
        nlayers_to_pass = {u: self.nb_layers for u in states_to_pass}
        for u in states_to_pass:
            for l in range(nlayers_to_pass[u]):
                ds_factor = 2 ** l
                nb_row = init_nb_row // ds_factor
                nb_col = init_nb_col // ds_factor
                if u in ['r', 'c']:
                    stack_size = self.R_stack_sizes[l]
                elif u == 'e':
                    stack_size = 2 * self.stack_sizes[l]
                output_size = stack_size * nb_row * nb_col
                initial_state = tf.zeros((batch, output_size))
                output_shp = (batch, nb_row, nb_col, stack_size)
                initial_state = tf.reshape(initial_state, output_shp)
                initial_states += [initial_state]
        return initial_states
    
    def step(self, a, states, reuse_step, scope_step='one_step'):
        r_tm1 = states[: self.nb_layers]
        c_tm1 = states[self.nb_layers: 2 * self.nb_layers]
        e_tm1 = states[2*self.nb_layers: 3*self.nb_layers]

        c = []
        r = []
        e = []
        
        with tf.variable_scope(scope_step) as scope:
            if reuse_step:
                scope.reuse_variables()

            for l in reversed(range(self.nb_layers)):
                inputs = [r_tm1[l], e_tm1[l]]
                if l < self.nb_layers - 1:
                    inputs.append(r_up)
                
                inputs = tf.concat(inputs, axis=-1)
                _c, _r = self.convlstm(inputs, l, c_tm1[l], 'lstm' + str(l))
                c.insert(0, _c)
                r.insert(0, _r)

                if l > 0:
                    r_up = add_upscale(_r)
            
            for l in range(self.nb_layers):
                ahat = conv2d(r[l], self.stack_sizes[l], self.Ahat_filter_sizes[l], self.Ahat_filter_sizes[l], name="conv_ahat" + str(l))
                ahat = tf.nn.relu(ahat)
                if l == 0:
                    ahat = tf.minimum(ahat, self.pixel_max)
                    frame_prediction = ahat

                e_up = tf.nn.relu(ahat - a)
                e_down = tf.nn.relu(a - ahat)
                e.append(tf.concat([e_up, e_down], axis=-1))

                if self.output_layer_num == l:
                    if self.output_layer_type == 'A':
                        output = a
                    elif self.output_layer_type == 'Ahat':
                        output = ahat
                    elif self.output_layer_type == 'R':
                        output = r[l]
                    elif self.output_layer_type == 'E':
                        output = e[l]
                if l < self.nb_layers - 1:
                    a = conv2d(e[l], self.stack_sizes[l+1], self.A_filter_sizes[l], self.A_filter_sizes[l], name="conv_a"+str(l))
                    a = tf.nn.relu(a)
                    a = maxpool2d(a)

            if self.output_layer_type is None:
                if self.output_mode == 'prediction':
                    output = frame_prediction
                else:
                    for l in range(self.nb_layers):
                        layer_error = tf.reduce_mean(flatten(e[l]), axis=-1, keep_dims=True)
                        all_error = layer_error if l==0 else tf.concat([all_error, layer_error], axis=-1)
                    if self.output_mode == 'error':
                        output = all_error
                    else:
                        #output = tf.concat([flatten(frame_prediction), all_error], axis=-1)
                        output = [frame_prediction, all_error]

            states = r + c + e
            return output, states


    def convlstm(self, inputs, l, c, scope_name, reuse=False):
        with tf.variable_scope(scope_name) as scope:
            if reuse:
                scope.reuse_variables()

            concat = conv2d(inputs, self.R_stack_sizes[l] * 4, self.R_filter_sizes[l], self.R_filter_sizes[l], name='lstm')
            i, j, f, o = tf.split(axis=3, num_or_size_splits=4, value=concat)
            new_c = (c * tf.nn.sigmoid(f)) + tf.nn.sigmoid(i) * tf.nn.tanh(j)
            new_h = tf.nn.tanh(new_c) * tf.nn.sigmoid(o)
            return new_c, new_h

