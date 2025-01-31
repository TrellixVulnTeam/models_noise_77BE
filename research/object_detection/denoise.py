# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Detection model evaluator.

This file provides a generic evaluation method that can be used to evaluate a
DetectionModel.
"""

import tensorflow as tf

from collections import namedtuple
from module import *
import re
TOWER_NAME = 'tower'

def activation_summary(x):
  """Helper to create summaries for activations.

  Creates a summary that provides a histogram of activations.
  Creates a summary that measures the sparsity of activations.

  Args:
    x: Tensor
  Returns:
    nothing
  """
  # Remove 'tower_[0-9]/' from the name in case this is a multi-GPU training
  # session. This helps the clarity of presentation on tensorboard.
  tensor_name = re.sub('%s_[0-9]*/' % TOWER_NAME, '', x.op.name)
  tf.summary.histogram(tensor_name + '/activations', x)
  tf.summary.scalar(tensor_name + '/sparsity',
                                       tf.nn.zero_fraction(x))

def gated_denoise(images, FLAGS, is_training=False, noisy_batch_size=1):
  # Discriminator Network
  # We are just mixing the two images
  losses_dict = {}
  summaries_dict = {}

  OPTIONS = namedtuple('OPTIONS', 'gf_dim df_dim output_c_dim is_training')
  options = OPTIONS._make((FLAGS.ngf, FLAGS.ndf, 3, is_training))
  criterionGAN = mae_criterion
  criterionGAN2 = sce_criterion
  
  if is_training:
    noisy_images = images[:noisy_batch_size]
    original_images = images[noisy_batch_size:]

  # Filter
  summaries_dict['prefiltered_noisy_images'] = tf.summary.image('prefiltered_noisy_images', images, max_outputs=1)
  if is_training:
    summaries_dict['prefiltered_clean_images'] = tf.summary.image('prefiltered_clean_images', images[noisy_batch_size:], max_outputs=1)

  filtered_images = images
  if FLAGS.average_filter:
    print('average filter is applied')
    if FLAGS.mixture_of_filters:
      with tf.variable_scope('filter_gate') as scope:
        print('Building mixture of filters model')
        filtered_images0 = average_filter(images, 2)
        filtered_images1 = average_filter(images, 3)
        filtered_images2 = average_filter(images, 4)
        probs = gate(images, FLAGS.ndf, num_classes=4, reuse=False, name='gate')
        activation_summary(probs)
        filtered_images = tf.add_n([probs[:,0,None,None,None]*images,
                                    probs[:,1,None,None,None]*filtered_images0,
                                    probs[:,2,None,None,None]*filtered_images1,
                                    probs[:,3,None,None,None]*filtered_images2])
    else:
      filtered_images = average_filter(images, FLAGS.filter_size)
    summaries_dict['filtered_noisy_images'] = tf.summary.image('filtered_noisy_images', filtered_images, max_outputs=1)
    if is_training:
      summaries_dict['filtered_clean_images'] = tf.summary.image('filtered_clean_images', filtered_images[noisy_batch_size:], max_outputs=1)

  if FLAGS.denoise:
    print('denoise is applied')
    with tf.variable_scope('denoise') as scope:
      if FLAGS.generator_separate_channel:
        denoised_images = generator_separate_resnet(filtered_images, options, res_depth=FLAGS.res_depth, reuse=False, name='generator') 
      else:
        denoised_images = generator_resnet(filtered_images, options, res_depth=FLAGS.res_depth, output_c_dim=3, reuse=False, name='generator') 
      if is_training:
        denoise_sim_loss = abs_criterion(denoised_images[:noisy_batch_size], original_images, name='g_loss/sim_loss')
        g_loss = FLAGS.denoise_loss_factor * denoise_sim_loss
        losses_dict['g_loss'] = g_loss
        summaries_dict['g_loss/sim_loss'] = tf.summary.scalar('g_loss/sim_loss', denoise_sim_loss)
      if FLAGS.denoise_discrim:
        noisy_and_denoised = tf.concat([noisy_images, denoised_images], 3)
        d_denoised = discriminator(noisy_and_denoised, FLAGS.ndf, reuse=False, name='discriminator')
        if is_training:
          noisy_and_original = tf.concat([noisy_images, original_images], 3)
          d_original = discriminator(noisy_and_original, FLAGS.ndf, reuse=True,  name='discriminator')

          # generator loss
          g_gan_loss = criterionGAN(d_denoised, tf.ones_like(d_denoised))
          summaries_dict['g_gan_loss'] = tf.summary.scalar('g_loss/g_gan_loss', g_gan_loss)

          g_loss += FLAGS.denoise_gan_loss_factor * g_gan_loss
          losses_dict['g_loss'] = g_loss

          # discriminator loss
          d_loss_real = criterionGAN(d_original, tf.ones_like(d_original))
          d_loss_fake = criterionGAN(d_denoised, tf.zeros_like(d_denoised))

          d_loss = FLAGS.denoise_gan_loss_factor * (d_loss_real + d_loss_fake) / 2 
          losses_dict['d_loss'] = d_loss

          summaries_dict['d_loss'] = tf.summary.scalar('d_loss', d_loss)

      if is_training:
        summaries_dict['g_loss'] = tf.summary.scalar('g_loss', g_loss)
  
    filtered_images = denoised_images
    summaries_dict['filtered_noisy_images'] = tf.summary.image('filtered_noisy_images', filtered_images, max_outputs=1)
    if is_training:
      summaries_dict['filtered_clean_images'] = tf.summary.image('filtered_clean_images', filtered_images[noisy_batch_size:], max_outputs=1)

  # Gate Operation
  if FLAGS.discrim:
    print('gate network is applied')
    with tf.variable_scope('input_discrim') as scope:
      d_in_logits = discriminator(images, FLAGS.ndf, reuse=False, name='discriminator')

      if is_training:
        d_in_loss_real = criterionGAN2(d_in_logits[noisy_batch_size:],  tf.ones_like(d_in_logits[noisy_batch_size:]))
        d_in_loss_fake = criterionGAN2(d_in_logits[:noisy_batch_size], tf.zeros_like(d_in_logits[:noisy_batch_size]))

        d_in_loss = FLAGS.discrim_loss_factor * (d_in_loss_real + d_in_loss_fake) / 2 
        losses_dict['d_in_loss'] = d_in_loss
        summaries_dict['d_in_loss'] = tf.summary.scalar('d_in_loss', d_in_loss)
        #tf.add_to_collection('losses', losses_dict['d_in_loss'])
  
      d_in_logits = tf.reduce_mean(d_in_logits, [1, 2, 3])
      d_in_sigmoid = tf.nn.sigmoid(d_in_logits, name='is_clean')
      activation_summary(d_in_sigmoid)
      images =  tf.add(d_in_sigmoid[:,None,None,None] * images,
                       (1 - d_in_sigmoid[:,None,None,None]) * filtered_images)
  else:
    images = filtered_images

  summaries_dict['preprocessed_noisy_images'] = tf.summary.image('preprocessed_noisy_images', images, max_outputs=1)
  if is_training:
    summaries_dict['preprocessed_clean_images'] = tf.summary.image('preprocessed_clean_images', images[noisy_batch_size:], max_outputs=1)

  return images, losses_dict, summaries_dict


