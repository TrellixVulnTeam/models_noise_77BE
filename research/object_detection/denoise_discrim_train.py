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

r"""Training executable for detection models.

This executable is used to train DetectionModels. There are two ways of
configuring the training job:

1) A single pipeline_pb2.TrainEvalPipelineConfig configuration file
can be specified by --pipeline_config_path.

Example usage:
    ./train \
        --logtostderr \
        --train_dir=path/to/train_dir \
        --pipeline_config_path=pipeline_config.pbtxt

2) Three configuration files can be provided: a model_pb2.DetectionModel
configuration file to define what type of DetectionModel is being trained, an
input_reader_pb2.InputReader file to specify what training data will be used and
a train_pb2.TrainConfig file to configure training parameters.

Example usage:
    ./train \
        --logtostderr \
        --train_dir=path/to/train_dir \
        --model_config_path=model_config.pbtxt \
        --train_config_path=train_config.pbtxt \
        --input_config_path=train_input_config.pbtxt
"""

import functools
import json
import os
import tensorflow as tf

from object_detection import denoise_discrim_trainer
from object_detection.builders import input_reader_builder
from object_detection.builders import model_builder
from object_detection.utils import config_util

tf.logging.set_verbosity(tf.logging.INFO)

flags = tf.app.flags
flags.DEFINE_string('master', '', 'Name of the TensorFlow master to use.')
flags.DEFINE_integer('task', 0, 'task id')
flags.DEFINE_integer('num_clones', 1, 'Number of clones to deploy per worker.')
flags.DEFINE_boolean('clone_on_cpu', False,
                     'Force clones to be deployed on CPU.  Note that even if '
                     'set to False (allowing ops to run on gpu), some ops may '
                     'still be run on the CPU if they have no GPU kernel.')
flags.DEFINE_integer('worker_replicas', 1, 'Number of worker+trainer '
                     'replicas.')
flags.DEFINE_integer('ps_tasks', 0,
                     'Number of parameter server tasks. If None, does not use '
                     'a parameter server.')
flags.DEFINE_string('train_dir', '',
                    'Directory to save the checkpoints and training summaries.')

flags.DEFINE_string('pipeline_config_path', '',
                    'Path to a pipeline_pb2.TrainEvalPipelineConfig config '
                    'file. If provided, other configs are ignored')

flags.DEFINE_string('train_config_path', '',
                    'Path to a train_pb2.TrainConfig config file.')
flags.DEFINE_string('input_config_path', '',
                    'Path to an input_reader_pb2.InputReader config file.')
flags.DEFINE_string('model_config_path', '',
                    'Path to a model_pb2.DetectionModel config file.')

flags.DEFINE_boolean('generator_separate_channel', False,
                    'True for channel wise learning.')
flags.DEFINE_boolean('rand_stddev', True,
                    'True for random std deviation for noisy images.')
flags.DEFINE_integer('res_depth', 0, 'Depth of the residual block for the generator')
flags.DEFINE_boolean('resize', False,
                    'True for resizing the image.')
flags.DEFINE_boolean('denoise', False,
                    'True for denoise.')
flags.DEFINE_float('denoise_loss_factor', 1.,
                   'denoise loss factor [ 1.]')
flags.DEFINE_boolean('denoise_discrim', False,
                     'True for enabling discriminator network.')
flags.DEFINE_float('denoise_gan_loss_factor', 1.,
                   'discrim loss factor [ 0.01]')
flags.DEFINE_boolean('discrim', False,
                     'True for enabling discriminator network.')
flags.DEFINE_float('discrim_loss_factor', 1.,
                   'discrim loss factor [ 0.01]')
flags.DEFINE_integer('ks', 3,
                     'size of the convolutional filter')
flags.DEFINE_integer('ngf', 16,
                     '# of gen filters in first conv layer')
flags.DEFINE_integer('ndf', 16,
                     '# of discrim filters in first conv layer')


flags.DEFINE_string('similarity_loss', 'bidirection',
#flags.DEFINE_string('similarity_loss', 'pivot',
                    'Choose between pivot and bidirection.')
flags.DEFINE_float('similarity_loss_factor', 0.0,
                   'similarity loss factor [ 0.0001]')
flags.DEFINE_float('second_stage_similarity_loss_factor', 0.0,
                   'second stage similarity loss factor [ 0.0001]')

flags.DEFINE_boolean('additive_noise', False,
                     'True for adding noise additively.')
flags.DEFINE_boolean('lowres', False,
                     'True for low + high resolution training.')
flags.DEFINE_float('stddev', 0.15,
                   'stddev for gaussian noise assuming pixels are'
                   'in range [0, 1)')
flags.DEFINE_float('ratio', 0.1,
                   'ratio for salt_pepper noise')
flags.DEFINE_integer('filter_size', 3,
                   'filter size for median filter or average filter')
flags.DEFINE_boolean('median_filter', False,
                     'True for median filter use.')
flags.DEFINE_boolean('average_filter', False,
                     'True for average filter use.')
flags.DEFINE_boolean('mixture_of_filters', False,
                     'True for mixture of filters.')
flags.DEFINE_boolean('salt_pepper_noise', False,
                     'True for original + salt_pepper noise training/evaluation.')
flags.DEFINE_boolean('gaussian_noise', False,
                     'True for original + gaussian noise training.')
flags.DEFINE_boolean('snow', False,
                     'True for original + snow effect training.')

FLAGS = flags.FLAGS


def main(_):
  assert FLAGS.train_dir, '`train_dir` is missing.'
  if FLAGS.task == 0: tf.gfile.MakeDirs(FLAGS.train_dir)
  if FLAGS.pipeline_config_path:
    configs = config_util.get_configs_from_pipeline_file(
        FLAGS.pipeline_config_path)
    if FLAGS.task == 0:
      tf.gfile.Copy(FLAGS.pipeline_config_path,
                    os.path.join(FLAGS.train_dir, 'pipeline.config'),
                    overwrite=True)
  else:
    configs = config_util.get_configs_from_multiple_files(
        model_config_path=FLAGS.model_config_path,
        train_config_path=FLAGS.train_config_path,
        train_input_config_path=FLAGS.input_config_path)
    if FLAGS.task == 0:
      for name, config in [('model.config', FLAGS.model_config_path),
                           ('train.config', FLAGS.train_config_path),
                           ('input.config', FLAGS.input_config_path)]:
        tf.gfile.Copy(config, os.path.join(FLAGS.train_dir, name),
                      overwrite=True)

  model_config = configs['model']
  train_config = configs['train_config']
  input_config = configs['train_input_config']

  model_fn = functools.partial(
      model_builder.build,
      model_config=model_config,
      is_training=True)

  create_input_dict_fn = functools.partial(
      input_reader_builder.build, input_config)

  env = json.loads(os.environ.get('TF_CONFIG', '{}'))
  cluster_data = env.get('cluster', None)
  cluster = tf.train.ClusterSpec(cluster_data) if cluster_data else None
  task_data = env.get('task', None) or {'type': 'master', 'index': 0}
  task_info = type('TaskSpec', (object,), task_data)

  # Parameters for a single worker.
  ps_tasks = 0
  worker_replicas = 1
  worker_job_name = 'lonely_worker'
  task = 0
  is_chief = True
  master = ''

  if cluster_data and 'worker' in cluster_data:
    # Number of total worker replicas include "worker"s and the "master".
    worker_replicas = len(cluster_data['worker']) + 1
  if cluster_data and 'ps' in cluster_data:
    ps_tasks = len(cluster_data['ps'])

  if worker_replicas > 1 and ps_tasks < 1:
    raise ValueError('At least 1 ps task is needed for distributed training.')

  if worker_replicas >= 1 and ps_tasks > 0:
    # Set up distributed training.
    server = tf.train.Server(tf.train.ClusterSpec(cluster), protocol='grpc',
                             job_name=task_info.type,
                             task_index=task_info.index)
    if task_info.type == 'ps':
      server.join()
      return

    worker_job_name = '%s/task:%d' % (task_info.type, task_info.index)
    task = task_info.index
    is_chief = (task_info.type == 'master')
    master = server.target

  denoise_discrim_trainer.train(create_input_dict_fn, model_fn, train_config, master, task,
                FLAGS.num_clones, worker_replicas, FLAGS.clone_on_cpu, ps_tasks,
                worker_job_name, is_chief, FLAGS.train_dir)


if __name__ == '__main__':
  tf.app.run()
