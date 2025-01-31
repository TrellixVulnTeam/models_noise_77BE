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

import logging
import tensorflow as tf

from object_detection import eval_util
from object_detection.core import prefetcher
from object_detection.core import standard_fields as fields
from object_detection.utils import object_detection_evaluation
#from denoise import discrim, discrim_loss
from collections import namedtuple
from module import *
import re
import time
TOWER_NAME = 'tower'

flags = tf.app.flags
FLAGS = flags.FLAGS

# A dictionary of metric names to classes that implement the metric. The classes
# in the dictionary must implement
# utils.object_detection_evaluation.DetectionEvaluator interface.
EVAL_METRICS_CLASS_DICT = {
    'pascal_voc_metrics':
        object_detection_evaluation.PascalDetectionEvaluator,
    'weighted_pascal_voc_metrics':
        object_detection_evaluation.WeightedPascalDetectionEvaluator,
    'open_images_metrics':
        object_detection_evaluation.OpenImagesDetectionEvaluator
}

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

def get_median_filter_image(image, filter_size=3):

  image_shape = tf.shape(image)
  num_batch = image_shape[0]
  height = image_shape[1]
  width = image_shape[2]

  # uniform [1 - ratio, 2 - ratio)
  patches = tf.extract_image_patches(image, [1, filter_size, filter_size, 1],
                                     4*[1], 4*[1], 'SAME')
  medians = tf.contrib.distributions.percentile(patches, 50, axis=3)
  print (image.get_shape().as_list())
  print (patches.get_shape().as_list())
  print (medians.get_shape().as_list())

  return medians

def get_salt_pepper_noise_image(image, ratio=0.01, rand_stddev=False):

  image_shape = tf.shape(image)
  num_batch = image_shape[0]
  height = image_shape[1]
  width = image_shape[2]

  # uniform [1 - ratio, 2 - ratio)
  random_tensor = 1. - ratio
  random_tensor += tf.random_uniform(image_shape)
  # 0: ratio, 1: 1 - ratio
  binary_tensor = tf.floor(random_tensor)

  # scale image from [-1, 1] to [0, 1]
  random_tensor2 = tf.random_uniform(image_shape)
  image = (image + 1.)/2.
  noise_image = binary_tensor*image + (1.-binary_tensor)*random_tensor2

  # scaling pixel values to be in [-1, 1]).
  noise_image = tf.clip_by_value(noise_image*2.-1, -1, 1)
  return noise_image

def get_gaussian_noise_image(image, stddev=0.15, rand_stddev=False):

  image_shape = tf.shape(image)
  num_batch = image_shape[0]
  height = image_shape[1]
  width = image_shape[2]
  
  if rand_stddev:
    sigma = tf.abs(tf.truncated_normal([num_batch], mean=0., stddev=stddev/2))
  else:
    sigma = stddev
  # since preprocessed input values are in [-1, 1], we use sigma*2
  noise = tf.random_normal(image_shape, mean=0., stddev=sigma*2)
  # scaling pixel values to be in [-1, 1]).
  noise_image = tf.clip_by_value(image + noise, -1, 1)
  return noise_image

def get_lowres_image(image, factor=1, method=0, upsample=True):
  # image shape: [1, None, None, 3]
  image_shape = tf.shape(image)
  height = image_shape[1]
  width = image_shape[2]

  # subsample
  down_height = tf.to_int32(height/factor)
  down_width = tf.to_int32(width/factor)
  downsampled_image = tf.image.resize_images(image,
                                        [down_height, down_width])

  # resize
  upsampled_image = downsampled_image
  if upsample:
    upsampled_image = tf.image.resize_images(downsampled_image,
                                            [height, width], method=method)
  return upsampled_image

def average_filter(images, filter_size):
  images = tf.nn.avg_pool(images,
                          ksize=[1, filter_size, filter_size, 1],
                          strides=4*[1], padding='SAME')
  return images

def resize_dividable_image(image, number):
  # image shape: [1, None, None, 3]

  image_shape = tf.shape(image)
  height = image_shape[1]/number*number
  width = image_shape[2]/number*number
  return tf.image.resize_images(image, [height, width])
  
def gated_denoise(images, is_training, FLAGS):
  # Discriminator Network
  # We are just mixing the two images
  losses_dict = {}
  summaries_dict = {}

  OPTIONS = namedtuple('OPTIONS', 'gf_dim df_dim output_c_dim is_training')
  options = OPTIONS._make((FLAGS.ngf, FLAGS.ndf, 3, is_training))
  criterionGAN = mae_criterion
  criterionGAN2 = sce_criterion
  
  # Filter
  summaries_dict['prefiltered_noisy_images'] = tf.summary.image('prefiltered_noisy_images', images, max_outputs=1)

  filtered_images = images
  if FLAGS.average_filter:
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

  if FLAGS.denoise:
    with tf.variable_scope('denoise') as scope:
      if FLAGS.generator_separate_channel:
        denoised_images = generator_separate_resnet(filtered_images, options, res_depth=FLAGS.res_depth, reuse=False, name='generator') 
      else:
        denoised_images = generator_resnet(filtered_images, options, res_depth=FLAGS.res_depth, output_c_dim=3, reuse=False, name='generator') 
      if FLAGS.denoise_discrim:
        noisy_and_denoised = tf.concat([noisy_images, denoised_images], 3)
        d_denoised = discriminator(denoised, FLAGS.ndf, FLAGS.ks, reuse=False, name='discriminator')

    filtered_images = denoised_images
    summaries_dict['filtered_noisy_images'] = tf.summary.image('filtered_noisy_images', filtered_images, max_outputs=1)

  # Gate Operation
  if FLAGS.discrim:
    with tf.variable_scope('input_discrim') as scope:
      d_in_logits = discriminator(images, FLAGS.ndf, FLAGS.ks, reuse=False, name='discriminator')
      d_in_logits = tf.reduce_mean(d_in_logits, [1, 2, 3])
      d_in_sigmoid = tf.nn.sigmoid(d_in_logits, name='is_clean')
      activation_summary(d_in_sigmoid)
      images =  tf.add(d_in_sigmoid[:,None,None,None] * images,
                       (1 - d_in_sigmoid[:,None,None,None]) * filtered_images)
  else:
    images = filtered_images

  summaries_dict['preprocessed_noisy_images'] = tf.summary.image('preprocessed_noisy_images', images, max_outputs=1)

  return images, filtered_images, losses_dict, summaries_dict

def inception_preprocess(images):
  """ [0, 255] --> [-1, 1]
  """
  return (2.0 / 255.0) * images - 1.0 

def inception_depreprocess(images):
  """ [-1, 1] --> [0, 255]
  """
  return (255.0 / 2.0) * (images + 1.0)

def _extract_prediction_tensors(model,
                                create_input_dict_fn,
                                ignore_groundtruth=False):
  """Restores the model in a tensorflow session.

  Args:
    model: model to perform predictions with.
    create_input_dict_fn: function to create input tensor dictionaries.
    ignore_groundtruth: whether groundtruth should be ignored.

  Returns:
    tensor_dict: A tensor dictionary with evaluations.
  """
  input_dict = create_input_dict_fn()
  prefetch_queue = prefetcher.prefetch(input_dict, capacity=500)
  input_dict = prefetch_queue.dequeue()
  original_image = tf.expand_dims(input_dict[fields.InputDataFields.image], 0)


  preprocessed_image = inception_preprocess(tf.to_float(original_image))

  tf.summary.image('preprocessed_images', preprocessed_image[0])

  if FLAGS.salt_pepper_noise:
    preprocessed_image = get_salt_pepper_noise_image(preprocessed_image,
                                                     ratio=FLAGS.ratio)
    tf.summary.image('salt_pepper_noise_images', preprocessed_image[0])
  if FLAGS.gaussian_noise:
    preprocessed_image = get_gaussian_noise_image(preprocessed_image,
                                                  stddev=FLAGS.stddev)
    tf.summary.image('gaussian_noise_images', preprocessed_image[0])
  if FLAGS.lowres:
    preprocessed_image = get_lowres_image(preprocessed_image,
                                    factor=FLAGS.subsample_factor,
                                    method=FLAGS.resize_method,
                                    upsample=FLAGS.upsample)
    tf.summary.image('lowres_images', preprocessed_image[0])
  
  # prefiltered_image
  prefiltered_image = preprocessed_image
  tf.summary.image('prefiltered_images', prefiltered_image[0])

#  # apply filter here
#  if FLAGS.median_filter:
#    preprocessed_image = get_median_filter_image(preprocessed_image,
#                                                 filter_size=FLAGS.filter_size)
#    tf.summary.image('median_filter_images', preprocessed_image[0])
#
#  if FLAGS.average_filter:
#    preprocessed_image = tf.nn.avg_pool(preprocessed_image,
#                            ksize=[1, FLAGS.filter_size, FLAGS.filter_size, 1],
#                            strides=4*[1], padding='SAME')
#    tf.summary.image('average_filter_images', preprocessed_image[0])
#    if FLAGS.discrim:
#      with tf.variable_scope('discrim') as scope:
#        discrim_logits, _ = discrim(prefiltered_image,
#                                    train_batch_norm=model._is_training)
#        discrim_softmax = tf.nn.softmax(discrim_logits, name='softmax')
#        preprocessed_image =  tf.add(discrim_softmax[:,0,None,None,None]*prefiltered_image,
#                                     discrim_softmax[:,1,None,None,None]*preprocessed_image)
#
#  # Denoise
#  prediction_dict = model.denoise(preprocessed_image)
#  preprocessed_image = prediction_dict['denoised_images']


  # Gated denoise
  preprocessed_image = resize_dividable_image(preprocessed_image, 4)
  preprocessed_image, filtered_image, losses_dict, summaries_dict = gated_denoise(preprocessed_image, False, FLAGS)
  prediction_dict = {}
  preprocessed_image_for_summary = preprocessed_image

  # deprocess and preprocess with model's preprocess
  preprocessed_image = inception_depreprocess(preprocessed_image)
  preprocessed_image = model.preprocess(preprocessed_image)
#  prediction_dict = model.predict(preprocessed_image)
  prediction_dict.update(model.predict(preprocessed_image))
  detections = model.postprocess(prediction_dict)

  # original image
  # change preprocessed_image to uint8 image tensor
  filtered_image = tf.to_int32(255 * (filtered_image + 1) / 2)
  prefiltered_image = tf.to_int32(255 * (prefiltered_image + 1) / 2)
  preprocessed_image = tf.to_int32(255 * (preprocessed_image_for_summary + 1) / 2)

  groundtruth = None
  if not ignore_groundtruth:
    groundtruth = {
        fields.InputDataFields.groundtruth_boxes:
            input_dict[fields.InputDataFields.groundtruth_boxes],
        fields.InputDataFields.groundtruth_classes:
            input_dict[fields.InputDataFields.groundtruth_classes],
        fields.InputDataFields.groundtruth_area:
            input_dict[fields.InputDataFields.groundtruth_area],
        fields.InputDataFields.groundtruth_is_crowd:
            input_dict[fields.InputDataFields.groundtruth_is_crowd],
        fields.InputDataFields.groundtruth_difficult:
            input_dict[fields.InputDataFields.groundtruth_difficult]
    }
    if fields.InputDataFields.groundtruth_group_of in input_dict:
      groundtruth[fields.InputDataFields.groundtruth_group_of] = (
          input_dict[fields.InputDataFields.groundtruth_group_of])
    if fields.DetectionResultFields.detection_masks in detections:
      groundtruth[fields.InputDataFields.groundtruth_instance_masks] = (
          input_dict[fields.InputDataFields.groundtruth_instance_masks])

  return eval_util.result_dict_for_single_example(
      original_image,
      prefiltered_image,
      filtered_image,
      preprocessed_image,
      input_dict[fields.InputDataFields.source_id],
      detections,
      groundtruth,
      class_agnostic=(
          fields.DetectionResultFields.detection_classes not in detections),
      scale_to_absolute=True)


def get_evaluators(eval_config, categories, matching_iou_thresholds=None):
  """Returns the evaluator class according to eval_config, valid for categories.

  Args:
    eval_config: evaluation configurations.
    categories: a list of categories to evaluate.
  Returns:
    An list of instances of DetectionEvaluator.

  Raises:
    ValueError: if metric is not in the metric class dictionary.
  """
  eval_metric_fn_key = eval_config.metrics_set
  if eval_metric_fn_key not in EVAL_METRICS_CLASS_DICT:
    raise ValueError('Metric not found: {}'.format(eval_metric_fn_key))
  if matching_iou_thresholds is not None:
    result = [EVAL_METRICS_CLASS_DICT[eval_metric_fn_key](
              categories=categories, matching_iou_threshold=threshold)
              for threshold in matching_iou_thresholds]
    return result
  else:
    return [EVAL_METRICS_CLASS_DICT[eval_metric_fn_key](
          categories=categories)]


def evaluate(create_input_dict_fn, create_model_fn, eval_config, categories,
             matching_iou_thresholds, checkpoint_dir, eval_dir):
  """Evaluation function for detection models.

  Args:
    create_input_dict_fn: a function to create a tensor input dictionary.
    create_model_fn: a function that creates a DetectionModel.
    eval_config: a eval_pb2.EvalConfig protobuf.
    categories: a list of category dictionaries. Each dict in the list should
                have an integer 'id' field and string 'name' field.
    matching_iou_thresholds: list of IOU threshold to use for matching
                             groundtruth boxes to detection boxes.
    checkpoint_dir: directory to load the checkpoints to evaluate from.
    eval_dir: directory to write evaluation metrics summary to.

  Returns:
    metrics: A dictionary containing metric names and values from the latest
      run.
  """

  model = create_model_fn()

  if eval_config.ignore_groundtruth and not eval_config.export_path:
    logging.fatal('If ignore_groundtruth=True then an export_path is '
                  'required. Aborting!!!')

  tensor_dict = _extract_prediction_tensors(
      model=model,
      create_input_dict_fn=create_input_dict_fn,
      ignore_groundtruth=eval_config.ignore_groundtruth)

  def _process_batch(tensor_dict, sess, batch_index, counters):
    """Evaluates tensors in tensor_dict, visualizing the first K examples.

    This function calls sess.run on tensor_dict, evaluating the original_image
    tensor only on the first K examples and visualizing detections overlaid
    on this original_image.

    Args:
      tensor_dict: a dictionary of tensors
      sess: tensorflow session
      batch_index: the index of the batch amongst all batches in the run.
      counters: a dictionary holding 'success' and 'skipped' fields which can
        be updated to keep track of number of successful and failed runs,
        respectively.  If these fields are not updated, then the success/skipped
        counter values shown at the end of evaluation will be incorrect.

    Returns:
      result_dict: a dictionary of numpy arrays
    """
    try:
      result_dict = sess.run(tensor_dict)
      counters['success'] += 1
    except tf.errors.InvalidArgumentError:
      logging.info('Skipping image')
      counters['skipped'] += 1
      return {}
    global_step = tf.train.global_step(sess, tf.train.get_global_step())
    if batch_index < eval_config.num_visualizations:
      tag = 'image-{}'.format(batch_index)
      eval_util.visualize_detection_results(
          result_dict,
          tag,
          global_step,
          categories=categories,
          summary_dir=eval_dir,
          export_dir=eval_config.visualization_export_dir,
          show_groundtruth=eval_config.visualization_export_dir)
    return result_dict

  variables_to_restore = tf.global_variables()
  global_step = tf.train.get_or_create_global_step()
  variables_to_restore.append(global_step)
  if eval_config.use_moving_averages:
    variable_averages = tf.train.ExponentialMovingAverage(0.0)
    variables_to_restore = variable_averages.variables_to_restore()
  saver = tf.train.Saver(variables_to_restore)

  def _restore_latest_checkpoint(sess):
    latest_checkpoint = tf.train.latest_checkpoint(checkpoint_dir)
    saver.restore(sess, latest_checkpoint)

  metrics = eval_util.repeated_checkpoint_run(
      tensor_dict=tensor_dict,
      summary_dir=eval_dir,
      evaluators=get_evaluators(eval_config, categories, matching_iou_thresholds),
      batch_processor=_process_batch,
      checkpoint_dirs=[checkpoint_dir],
      variables_to_restore=None,
      restore_fn=_restore_latest_checkpoint,
      num_batches=eval_config.num_examples,
      eval_interval_secs=eval_config.eval_interval_secs,
      max_number_of_evaluations=(1 if eval_config.ignore_groundtruth else
                                 eval_config.max_evals
                                 if eval_config.max_evals else None),
      master=eval_config.eval_master,
      save_graph=eval_config.save_graph,
      save_graph_dir=(eval_dir if eval_config.save_graph else ''))

  return metrics
