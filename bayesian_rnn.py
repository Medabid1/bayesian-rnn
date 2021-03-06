#!/usr/bin/env python
# -*- coding: utf-8 -*-

import tensorflow as tf
from tensorflow.contrib.rnn import static_rnn, LSTMStateTuple

from stochastic_variables import get_random_normal_variable, ExternallyParameterisedLSTM
from stochastic_variables import gaussian_mixture_nll
import logging

logger = logging.getLogger(__name__)


class BayesianRNN(object):

    """
    An implementation of an RNN trained using Variational Bayes for RNNs, introduced in:
    Bayesian Recurrent Neural Networks, Meire Fortunato, Charles Blundell, Oriol Vinyals.
    https://arxiv.org/abs/1704.02798.
    """

    def __init__(self, config, is_training=False):

        self.config = config
        self.batch_size = config.batch_size
        self.num_steps = config.num_steps
        self.hidden_size = config.hidden_size
        self.embedding_size = config.embedding_size
        self.vocab_size = config.vocab_size
        self.max_grad_norm = config.max_grad_norm
        self.learning_rate = config.learning_rate
        self.learning_rate_decay = config.learning_rate_decay
        self.init_scale = config.init_scale
        self.summary_frequency = config.summary_frequency
        self.is_training = is_training

    def build(self):

        logger.info("Building model")
        self.global_step = tf.Variable(0, name='global_step', trainable=False)
        self.build_rnn()

        if self.is_training:
            logger.info("Adding training operations")
            tvars = tf.trainable_variables()
            grads, _ = tf.clip_by_global_norm(tf.gradients(self.cost, tvars), self.max_grad_norm)

            self.learning_rate = tf.Variable(self.learning_rate, trainable=False)
            self._new_learning_rate = tf.placeholder(tf.float32, shape=[], name="new_learning_rate")
            self._lr_update = tf.assign(self.learning_rate, self._new_learning_rate)
            optimizer = tf.train.GradientDescentOptimizer(self.learning_rate)
            tf.summary.scalar("learning_rate", self.learning_rate)
            self.train_op = optimizer.apply_gradients(
                zip(grads, tvars), global_step=self.global_step, name='train_step')

        self.summary = tf.summary.merge_all()
        self.image_summary = tf.summary.merge_all("IMAGE")

    def build_rnn(self):
        # Placeholders for inputs.
        self.input_data = tf.placeholder(tf.int32, [self.batch_size, self.num_steps])
        self.targets = tf.placeholder(tf.int32, [self.batch_size, self.num_steps])
        self.initial_lstm_memory = tf.placeholder(tf.float32, [self.batch_size, self.hidden_size])
        self.initial_lstm_state = tf.placeholder(tf.float32, [self.batch_size, self.hidden_size])
        self.initial_state = LSTMStateTuple(self.initial_lstm_memory, self.initial_lstm_state)

        # Embed and split up input into a list of (batch_size, embedding_dim) tensors.
        embedding = tf.get_variable('embedding', [self.vocab_size, self.embedding_size])
        inputs = tf.nn.embedding_lookup(embedding, self.input_data)
        inputs = [tf.squeeze(single_input, [1]) for single_input in tf.split(inputs, self.config.num_steps, 1)]

        # Set up stochastic LSTM cell with weights drawn from q(phi) = N(phi | mu, sigma)
        logger.info("Building LSTM cell with weights drawn from q(phi) = N(phi | mu, sigma)")
        with tf.variable_scope("phi_rnn"):

            phi_w, phi_w_mean, phi_w_std = get_random_normal_variable("phi_w", 0.0, self.init_scale,
                                               [self.embedding_size + self.hidden_size,
                                                4 * self.hidden_size], dtype=tf.float32)
            phi_b, phi_b_mean, phi_b_std = get_random_normal_variable("phi_b", 0.0, self.init_scale,
                                               [4 * self.hidden_size], dtype=tf.float32)

            tf.summary.image("phi_mean", tf.reshape(phi_w_mean, [1, self.embedding_size + self.hidden_size,
                                                4 * self.hidden_size, 1]), max_outputs=1, collections=["IMAGE"])
            tf.summary.image("phi_std", tf.reshape(phi_w_std, [1, self.embedding_size + self.hidden_size,
                                                4 * self.hidden_size, 1]), max_outputs=1, collections=["IMAGE"])

            phi_cell = ExternallyParameterisedLSTM(phi_w, phi_b, num_units=self.hidden_size)

        with tf.variable_scope("softmax_weights"):
            softmax_w, softmax_w_mean, softmax_w_std = \
                get_random_normal_variable("softmax_w", 0.0, self.init_scale,
                                           [self.hidden_size, self.vocab_size], dtype=tf.float32)

            softmax_b, softmax_b_mean, softmax_b_std = \
                get_random_normal_variable("softmax_b", 0.0, self.init_scale,
                                           [self.vocab_size], dtype=tf.float32)

        # Sample from posterior and assign to LSTM weights
        logger.info("Resampling weights using Posterior Sharpening")
        posterior_weights = self.sharpen_posterior(inputs, phi_cell, [phi_w, phi_b], [softmax_w, softmax_b])
        [theta_w, theta_b] = posterior_weights[0]
        [posterior_softmax_w, posterior_softmax_b] = posterior_weights[1]
        [theta_w_mean, theta_b_mean] = posterior_weights[2]
        [posterior_softmax_w_mean, posterior_softmax_b_mean] = posterior_weights[3]

        tf.summary.image("sharpening_difference",
                         tf.reshape(theta_w_mean - phi_w_mean,
                                    [1, self.embedding_size + self.hidden_size, 4 * self.hidden_size, 1]),
                         max_outputs=1,
                         collections=["IMAGE"])

        logger.info("Building LSTM cell with new weights sampled from posterior")
        with tf.variable_scope("theta_lstm"):
            theta_cell = ExternallyParameterisedLSTM(theta_w, theta_b, num_units=self.hidden_size)

        outputs, final_state = static_rnn(theta_cell, inputs, initial_state=self.initial_state)

        self.final_lstm_memory = final_state.c
        self.final_lstm_state = final_state.h

        negative_log_likelihood = self.get_negative_log_likelihood(outputs,
                                                                   posterior_softmax_w,
                                                                   posterior_softmax_b)
        tf.summary.scalar("negative_log_likelihood", negative_log_likelihood)

        # KL(q(theta| mu, (x, y)) || p(theta | mu))
        # For each parameter, compute the KL divergence between the parameters exactly, as they are
        # parameterised using multivariate gaussians with diagonal covariance, meaning the KL between
        # them is a exact function of their means and standard deviations.
        theta_kl = 0.0
        for theta, phi in zip([theta_w_mean, theta_b_mean, posterior_softmax_w_mean, posterior_softmax_b_mean],
                              [phi_w_mean, phi_b_mean, softmax_w_mean, softmax_b_mean]):
            theta_kl += self.compute_kl_divergence((theta, 0.02), (phi, 0.02))

        tf.summary.scalar("theta_kl", theta_kl)

        # KL(q(phi) || p(phi))
        # Here we are using an _empirical_ approximation of the KL divergence
        # using a single sample, because we are parameterising p(phi) as a mixture of gaussians,
        # so the KL no longer has a closed form.
        phi_kl = 0.0
        for weight, mean, std in [[phi_w, phi_w_mean, phi_w_std],
                                  [phi_b, phi_b_mean, phi_b_std],
                                  [softmax_w, softmax_w_mean, softmax_w_std],
                                  [softmax_b, softmax_b_mean, softmax_b_std]]:

            # # TODO(Mark): get this to work with the MOG prior using sampling.
            # mean1 = mean2 = tf.zeros_like(mean)
            # # Very pointy one:
            # std1 = 0.0009 * tf.ones_like(std)
            # # Flatter one:
            # std2 = 0.15 * tf.ones_like(std)
            # phi_mixture_nll = gaussian_mixture_nll(weight, [0.6, 0.4], mean1, mean2, std1, std2)
            # phi_kl += phi_mixture_nll

            # This is different from the paper - just using a univariate gaussian
            # prior so that the KL has a closed form.
            phi_kl += self.compute_kl_divergence((mean, std), (tf.zeros_like(mean), tf.ones_like(std) * 0.01))

        tf.summary.scalar("phi_kl", phi_kl)

        self.cost = negative_log_likelihood + (theta_kl / self.batch_size) + (phi_kl / self.batch_size*self.num_steps)
        self.inference_cost = self.mean_field_inference(inputs, phi_w_mean, phi_b_mean, softmax_w_mean, softmax_b_mean)
        tf.summary.scalar("sharpened_word_perplexity", tf.minimum(1000.0, tf.exp(self.cost/self.num_steps)))
        tf.summary.scalar("unsharpened_val_perplexity", tf.exp(self.inference_cost/self.num_steps), "VAL")

    def sharpen_posterior(self, inputs, cell, cell_weights, softmax_weights):

        """
        We want to reduce the variance of the variational posterior q(theta) in order to speed up learning.
        In order to do this, we add some information about this specific minibatch into the posterior by
        modelling q(theta| (x,y)). This is the same thing that you might do when you use a VAE; you are
        using a neural network to encode the inputs into the parameters of a distribution,
        which you then sample from. Normally, your latent space might be 100 dimensions - here, it is every
        parameter in our LSTM, so using a neural network isn't going to work.

        Instead, we're going to compute the gradient of our current LSTM parameters and sample some new ones
        using a linear combination of the gradient and the current weights. Specifically, we are going to
        sample new weights theta from:

            theta ~ N(theta | phi - mu * delta, sigma*I)

        where:

            delta = gradient of -log(p(y|phi, x) with respect to phi, the weight and bias of the LSTM.

        :param inputs: A list of length num_steps of tensors of shape (batch_size, embedding_size).
                The minibatch of inputs we are sharpening the posterior around.
        :param cell: The LSTM cell initialised with the phi parameters.
        :param cell_weights: A tuple of (phi_w, phi_b), corresponding to the parameters used
                in all 4 gates of the LSTM cell.

        :return theta_weights, posterior_softmax_weights: A tuple of (theta_w, theta_b)/(softmax_w, softmax_b)
                  of the same respective shape as (phi_w, phi_b)/(softmax_w, softmax_b), parameterised as a
                  linear combination of phi and delta := -log(p(y|phi, x) by sampling from:
                  theta ~ N(theta| phi - mu * delta, sigma*I),where sigma is a hyperparameter and mu is
                  a "learning rate".

        :return theta_parameters/softmax_parameters: A tuple of (theta_w_mean, theta_b_mean)/
                  (softmax_w_mean, softmax) the mean of the normal distribution used to
                  sample theta (i.e  phi - mu * delta).
        """

        outputs, _ = static_rnn(cell, inputs, initial_state=self.initial_state)
        cost = self.get_negative_log_likelihood(outputs, *softmax_weights)

        all_weights = cell_weights + softmax_weights

        # Gradients of log(p(y | phi, x )) with respect to phi (i.e., the log likelihood).
        gradients, _ = tf.clip_by_global_norm(tf.gradients(cost, all_weights), self.max_grad_norm)
        new_weights = []
        new_parameters = []
        parameter_name_scopes = ["phi_w_sample", "phi_b_sample", "softmax_w_sample", "softmax_b_sample"]
        for (cell_weight, log_likelihood_grad, scope) in zip(all_weights, gradients, parameter_name_scopes):

            with tf.variable_scope(scope):  # We want each parameter to use different smoothing weights.
                new_hierarchical_posterior, new_posterior_mean = self.resample(cell_weight, log_likelihood_grad)

            new_weights.append(new_hierarchical_posterior)
            new_parameters.append(new_posterior_mean)

        theta_weights = new_weights[:2]
        posterior_softmax_weights = new_weights[2:]
        theta_parameters = new_parameters[:2]
        softmax_parameters = new_parameters[2:]

        return theta_weights, posterior_softmax_weights, theta_parameters, softmax_parameters

    @staticmethod
    def resample(weight, gradient):
        """
        Given parameters phi and the gradients of phi with respect to -log(p(y|phi, x),
        sample posterior weights: theta ~ N(theta | phi - mu * delta, sigma*I).

        :param weight:
        :param gradient:
        :return:
        """
        # Per parameter "learning rate" for the posterior parameterisation.
        smoothing_variable = tf.get_variable("posterior_mean_smoothing",
                                             shape=weight.get_shape(),
                                             initializer=tf.random_normal_initializer(stddev=0.01))
        # Here we are basically saying:
        # "if we had to choose another set of weights to use, they should probably be a
        # combination of what they are now and some gradient step with momentum towards
        # the loss of our objective wrt to these parameters. Plus a very little bit of noise."
        new_posterior_mean = weight - (smoothing_variable * gradient)
        new_posterior_std = 0.02 * tf.random_normal(weight.get_shape(), mean=0.0, stddev=1.0)
        new_hierarchical_posterior = new_posterior_mean + new_posterior_std

        return new_hierarchical_posterior, new_posterior_mean

    def get_negative_log_likelihood(self, outputs, softmax_w, softmax_b):

        """
        Given a sequence of outputs from an LSTM and projection weights to project the LSTM
        outputs to |V|, compute the batch averaged NLL.
        """
        output = tf.reshape(tf.concat(outputs, 1), [-1, self.hidden_size])
        logits = tf.matmul(output, softmax_w) + softmax_b   # dim (numsteps*batchsize, vocabsize)

        labels = tf.reshape(self.targets, [-1])
        labels = tf.one_hot(labels, self.vocab_size)
        # We can't use sparse_cross_entropy_loss as normal here because it's second derivative isn't
        # implmented in tensorflow yet (which we need because this loss is a function of the derivative
        # of the log likelihood wrt phi), so we have to create the actual 1-hot labels explicitly.
        loss = tf.nn.softmax_cross_entropy_with_logits(logits=logits, labels=labels)

        return tf.reduce_sum(loss) / self.batch_size

    @staticmethod
    def compute_kl_divergence(gaussian1, gaussian2):

        """
        Compute the batch averaged exact KL Divergence between two
         multivariate gaussians with diagonal covariance.

        :param gaussian1: (mean, std) of a multivariate gaussian.
        :param gaussian2: (mean, std) of a multivariate gaussian.
        :return: KL(gaussian1, gaussian2)
        """

        mean1, sigma1 = gaussian1
        mean2, sigma2 = gaussian2

        kl_divergence = tf.log(sigma2) - tf.log(sigma1) + \
                        ((tf.square(sigma1) + tf.square(mean1 - mean2)) / (2 * tf.square(sigma2))) \
                        - 0.5
        return tf.reduce_mean(kl_divergence)

    def mean_field_inference(self, inputs, mean_w, mean_b, softmax_w, softmax_b):
        """
        Build an LSTM using the mean parameters - used for inference, because we can't run
        posterior sampling if we don't have labels!
        :return:
        """
        cell = ExternallyParameterisedLSTM(mean_w, mean_b, num_units=self.hidden_size)
        outputs, final_state = static_rnn(cell, inputs=inputs, initial_state=self.initial_state)

        self.final_lstm_state_val = final_state.h
        self.final_lstm_memory_val = final_state.c

        return self.get_negative_log_likelihood(outputs, softmax_w, softmax_b)

    def decay_learning_rate(self, sess):
        learning_rate = sess.run(self.learning_rate)
        new_learning_rate = learning_rate * self.learning_rate_decay
        sess.run(self._lr_update, {self._new_learning_rate: new_learning_rate})

    def run_train_step(self, sess, inputs, targets, state, memory, step):

        if step % self.summary_frequency == 0:
            summary, cost, train_step, state, memory, _ = sess.run([self.summary, self.cost, self.global_step,
                                                                    self.final_lstm_state, self.final_lstm_memory,
                                                                    self.train_op],
                                                    {self.input_data: inputs, self.targets: targets,
                                                     self.initial_lstm_state: state, self.initial_lstm_memory: memory})
        else:
            cost, train_step, state, memory, _ = sess.run([self.cost, self.global_step, self.final_lstm_state,
                                                           self.final_lstm_memory, self.train_op],
                                           {self.input_data: inputs, self.targets: targets,
                                            self.initial_lstm_state: state, self.initial_lstm_memory: memory})
            summary = None
        return summary, cost, state, memory, train_step

    def run_eval_step(self, sess, inputs, targets, state, memory, step):

        if step % self.summary_frequency == 0:
            summary, cost, val_step, state, memory = sess.run([self.summary, self.inference_cost, self.global_step,
                                                 self.final_lstm_state_val, self.final_lstm_memory_val],
                                               {self.input_data: inputs, self.targets: targets,
                                                self.initial_lstm_state: state, self.initial_lstm_memory: memory})
        else:
            cost, val_step, state, memory = sess.run([self.inference_cost, self.global_step,
                                                      self.final_lstm_state_val, self.final_lstm_memory_val],
                                      {self.input_data: inputs, self.targets: targets,
                                       self.initial_lstm_state: state, self.initial_lstm_memory: memory})
            summary = None
        return summary, cost, state, memory, val_step

    def run_image_summary(self, sess, inputs, targets, state, memory):
        return sess.run([self.image_summary, self.global_step],
                        {self.input_data: inputs, self.targets: targets,
                         self.initial_lstm_state: state, self.initial_lstm_memory: memory})
