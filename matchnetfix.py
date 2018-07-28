import os
import numpy as np
import cv2
import tensorflow as tf
from omniloader import OmniglotLoader as og

class MatchNetFix():
    eps = 1e-10
    learn_rate = 5e-6
    global_step = tf.Variable(0, trainable=False, name='global_step')
    conv_param = {'k_sz':3, 'f_sz':64, 'c_sz':1, 'n_stack':4}
    k_shot = 1
    n_way = 5
    n_supports = k_shot*n_way    
    x_i = tf.placeholder(tf.float32, shape=[None, n_supports, og.im_size, og.im_size, og.im_channel])
    y_i_idx = tf.placeholder(tf.int32, shape=[None, n_supports]) # batch size, n_support
    x_hat = tf.placeholder(tf.float32, shape=[None, og.im_size, og.im_size, og.im_channel])
    y_hat_idx = tf.placeholder(tf.int32, shape=[None,]) # batch size
    
    y_i = tf.one_hot(y_i_idx, n_way)
    y_hat = tf.one_hot(y_hat_idx, n_way)

    def convnet_encoder(self, inputs, reuse=False):
        k_sz = self.conv_param['k_sz']
        f_sz = self.conv_param['f_sz']
        c_sz = self.conv_param['c_sz']
        n_stack = self.conv_param['n_stack']
        layer = inputs
        for l in range(n_stack):
            with tf.variable_scope('conv_{}'.format(l)):
                filters = tf.get_variable('filter', [k_sz, k_sz, c_sz, f_sz])
                beta = tf.get_variable('BN_beta', [f_sz], initializer=tf.constant_initializer(0.0))
                gamma = tf.get_variable('BN_gamma', [f_sz], initializer=tf.constant_initializer(1.0))
                c_sz = f_sz
                Z = tf.nn.conv2d(layer, filters, strides=[1,1,1,1], padding='SAME')
                mu, sig = tf.nn.moments(Z, [0,1,2])
                Z_tild = tf.nn.batch_normalization(Z, mu, sig, beta, gamma, self.eps)
                activ = tf.nn.relu(Z_tild)
                layer = tf.nn.max_pool(activ, ksize=[1,2,2,1], strides=[1,2,2,1], padding='VALID')
        return tf.squeeze(layer, [1,2])

    def __init__(self, share_encoder=True):
        self.sharing = share_encoder
        scope = 'image_encoder'
        with tf.variable_scope(scope):
            self.x_hat_encoded = self.convnet_encoder(self.x_hat)
        self.debug = tf.Print(self.x_hat_encoded, [tf.shape(self.x_hat_encoded), self.x_hat_encoded],'x_hat: ')

        if not self.sharing:
            scope = 'support_set_encoder'
        
        self.cos_sim_list = []
        with tf.variable_scope(scope, reuse=self.sharing):
            for i in range(self.n_supports):
                x_i_encoded = self.convnet_encoder(self.x_i[:,i,:,:,:], self.sharing)
                # self.debug = tf.Print(x_i_encoded, [tf.shape(x_i_encoded), x_i_encoded],'x_encoded: ')
                x_i_inv_mag = tf.rsqrt(tf.clip_by_value(tf.reduce_sum(tf.square(x_i_encoded),1,keepdims=True),self.eps,float('inf')))
                dotted = tf.squeeze(tf.matmul(tf.expand_dims(self.x_hat_encoded,1), tf.expand_dims(x_i_encoded,2)),[1,])
                self.cos_sim_list.append(dotted*x_i_inv_mag)
        
        cos_sim = tf.concat(axis=1, values=self.cos_sim_list)
        attention = tf.nn.softmax(cos_sim)
        self.prob = tf.squeeze(tf.matmul(tf.expand_dims(attention,1), self.y_i),[1])
        self.top_k = tf.nn.in_top_k(self.prob, self.y_hat_idx, 1)
        self.accuracy = tf.reduce_mean(tf.to_float(self.top_k))
        
        correct_prob = tf.reduce_sum(tf.log( tf.clip_by_value(self.prob, self.eps, 1.0))*self.y_hat, 1)        
        self.loss = tf.reduce_mean(-correct_prob, 0)
        optim = tf.train.AdamOptimizer(learning_rate=self.learn_rate)
        grad = optim.compute_gradients(self.loss)
        self.train_step = optim.apply_gradients(grad)

if __name__ == '__main__':
    
    loader = og(0)
    model = MatchNetFix()    
    session = tf.Session()
    session.run(tf.global_variables_initializer())
    print(session.run(tf.report_uninitialized_variables()))

    step, acc_batch = 0, 100
    acc_train, acc_loss, acc_test = [], [], []
    while True:
        batch_size = 1
        N_way = 5
        k_shot = 1
        x_support, y_support, x_query, y_query = loader.getTrainSample(batch_size, N_way, k_shot)
        [ _,loss_, prob_, top_k_, acc_ ] = session.run([model.train_step, model.loss, 
                model.prob, model.top_k, model.accuracy], feed_dict={
                model.x_i: x_support, model.y_i_idx: y_support,
                model.x_hat: x_query, model.y_hat_idx: y_query })
        
        n_try, all_n, n_epoch = loader.getStatus()
        # print('{}({}): {:2.2%}, {}'.format(step, n_epoch, acc_, loss_))

        if n_epoch % 10 == 0 and n_epoch != 0:
            x_support_test, y_support_test, x_query_test, y_query_test, origin_i, origin_hat = loader.getTestSample(batch_size, N_way, k_shot)
            [prob_t, top_k_t, acc_t] = session.run([model.prob, model.top_k, model.accuracy], feed_dict={
                model.x_i: x_support_test, model.y_i_idx: y_support_test,
                model.x_hat: x_query_test, model.y_hat_idx: y_query_test })
            
            acc_test.append(top_k_t[0])
            if len(acc_test) == acc_batch:
                acc_tmp = np.array(acc_test)*1
                acc_m = np.sum(acc_tmp)/acc_batch
                print('\ttest accuracy: {:2.2%}'.format(acc_m))
                acc_test.clear()

            # print('\ttest accuracy: {:2.2%}'.format(acc_t))
            for k, k_b in enumerate(top_k_t):
                if k_b:
                    max_loc = np.argmax(prob_t[k])
                    clss = [origin_i[k][chk] for chk in origin_i[k] if origin_i[k][chk][0]==max_loc]
                    for c_j in clss:
                        if c_j[1] != clss[0][1] or c_j[1] != origin_hat[k][1]:
                            print('error 1')

        acc_train.append(top_k_[0])
        acc_loss.append(loss_)
        if step % acc_batch == 0 and step != 0:
            num_s = len(acc_loss)
            loss_m = sum(acc_loss)/num_s
            acc_temp = np.array(acc_train)*1
            acc_m = np.sum(acc_temp)/num_s
            print('{}({}): {:2.2%}, {}'.format(step, n_epoch, acc_m, loss_m))
            acc_train.clear()
            acc_loss.clear()
        step += 1     

