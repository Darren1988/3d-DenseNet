import os
import time
import shutil
import platform
from datetime import timedelta

import numpy as np
import tensorflow as tf


TF_VERSION = float('.'.join(tf.__version__.split('.')[:2]))


class DenseNet3D(object):
  def __init__(self, data_provider, growth_rate, depth,
         total_blocks, keep_prob, dataset,
         weight_decay, nesterov_momentum, model_type, 
         should_save_logs, should_save_model,
         renew_logs=False, reduction=1.0, bc_mode=False,
         **kwargs):
    """
    Class to implement networks base on this paper
    https://arxiv.org/pdf/1611.05552.pdf

    Args:
      data_provider: Class, that have all required data sets
      growth_rate: `int`, variable from paper
      depth: `int`, variable from paper
      total_blocks: `int`, paper value == 3
      keep_prob: `float`, keep probability for dropout. If keep_prob = 1
        dropout will be disables
      weight_decay: `float`, weight decay for L2 loss, paper = 1e-4
      nesterov_momentum: `float`, momentum for Nesterov optimizer
      model_type: `str`, 'DenseNet' or 'DenseNet-BC'. Should model use
        bottle neck connections or not.
      dataset: `str`, dataset name
      should_save_logs: `bool`, should logs be saved or not
      should_save_model: `bool`, should model be saved or not
      renew_logs: `bool`, remove previous logs for current model
      reduction: `float`, reduction Theta at transition layer for
        DenseNets with bottleneck layers. See paragraph 'Compression'
        https://arxiv.org/pdf/1608.06993v3.pdf#4
      bc_mode: `bool`, should we use bottleneck layers and features
        reduction or not.
    """
    self.data_provider          = data_provider
    self.data_shape             = data_provider.data_shape
    self.n_classes              = data_provider.n_classes
    self.depth                  = depth
    self.growth_rate            = growth_rate
    # how many features will be received after first convolution
    # value the same as in the original Torch code
    self.first_output_features  = growth_rate * 2
    self.total_blocks           = total_blocks
    self.layers_per_block       = (depth - (total_blocks + 1)) // total_blocks
    self.bc_mode = bc_mode
    # compression rate at the transition layers
    self.reduction              = reduction
    if not bc_mode:
      print("Build %s model with %d blocks, "
          "%d composite layers each." % (
            model_type, self.total_blocks, self.layers_per_block))
    if bc_mode:
      self.layers_per_block     = self.layers_per_block // 2
      print("Build %s model with %d blocks, "
          "%d bottleneck layers and %d composite layers each." % (
            model_type, self.total_blocks, self.layers_per_block,
            self.layers_per_block))
    print("Reduction at transition layers: %.1f" % self.reduction)

    self.keep_prob          = keep_prob
    self.weight_decay       = weight_decay
    self.nesterov_momentum  = nesterov_momentum
    self.model_type         = model_type
    self.dataset_name       = dataset
    self.should_save_logs   = should_save_logs
    self.should_save_model  = should_save_model
    self.renew_logs         = renew_logs
    self.batches_step       = 0

    self._define_inputs()
    self._build_graph()
    self._initialize_session()
    self._count_trainable_params()

  # (Updated)
  def _initialize_session(self):
    """Initialize session, variables, saver"""
    config = tf.ConfigProto()
    # restrict model GPU memory utilization to min required
    config.gpu_options.allow_growth = True
    self.sess = tf.Session(config=config)
    tf_ver = int(tf.__version__.split('.')[1])
    if TF_VERSION <= 0.10:
      self.sess.run(tf.initialize_all_variables())
      logswriter = tf.train.SummaryWriter
    else:
      self.sess.run(tf.global_variables_initializer())
      logswriter = tf.summary.FileWriter
    self.saver = tf.train.Saver(tf.global_variables(), max_to_keep=0)
    self.summary_writer = logswriter(self.logs_path, self.sess.graph)

  # (Updated)
  def _count_trainable_params(self):
    total_parameters = 0
    for variable in tf.trainable_variables():
      shape = variable.get_shape()
      variable_parametes = 1
      for dim in shape:
        variable_parametes *= dim.value
      total_parameters += variable_parametes
    print("Total training params: %.1fM" % (total_parameters / 1e6))

  @property
  def save_path(self):
    try:
      save_path = self._save_path
      model_path = self._model_path
    except AttributeError:
      save_path = 'saves/%s' % self.model_identifier
      if platform.python_version_tuple()[0] is '2':
        if not os.path.exists(save_path):
          os.makedirs(save_path)
      else:
        os.makedirs(save_path, exist_ok=True)
      model_path = os.path.join(save_path, 'model.chkpt')
      self._save_path = save_path
      self._model_path = model_path
    return save_path, model_path

  @property
  def logs_path(self):
    try:
      logs_path = self._logs_path
    except AttributeError:
      logs_path = 'logs/%s' % self.model_identifier
      if self.renew_logs:
        shutil.rmtree(logs_path, ignore_errors=True)
      if platform.python_version_tuple()[0] is '2':
        if not os.path.exists(logs_path):
          os.makedirs(logs_path)
      else:
        os.makedirs(logs_path, exist_ok=True)
      self._logs_path = logs_path
    return logs_path

  @property
  def model_identifier(self):
    return "{}_growth_rate={}_depth={}_dataset_{}_total_block={}".format(
      self.model_type, self.growth_rate, self.depth, self.dataset_name, self.total_blocks)

  # (Updated)
  def save_model(self, global_step=None):
    self.saver.save(self.sess, self.save_path[1], global_step=global_step)

  def load_model(self):
    """load the sess from the pretrain model

      Returns:
        start_epoch: the start step to train the model
    """
    # Restore the trianing model from the folder
    ckpt =  tf.train.get_checkpoint_state(self.save_path[0])
    if ckpt and ckpt.model_checkpoint_path:
      self.saver.restore(self.sess, ckpt.model_checkpoint_path)
      start_epoch = ckpt.model_checkpoint_path.split('/')[-1].split('-')[-1]
      start_epoch = int(start_epoch) + 1
      print("Successfully load model from save path: %s and epoch: %s" 
          % (self.save_path[0], start_epoch))
      return start_epoch
    else:
      print("Training from scratch")
      return 1

  # (Updated)
  def log_loss_accuracy(self, loss, accuracy, epoch, prefix,
                        should_print=True):
    if should_print:
      print("mean cross_entropy: %f, mean accuracy: %f" % (
        loss, accuracy))
    summary = tf.Summary(value=[
      tf.Summary.Value(
        tag='loss_%s' % prefix, simple_value=float(loss)),
      tf.Summary.Value(
        tag='accuracy_%s' % prefix, simple_value=float(accuracy))
    ])
    self.summary_writer.add_summary(summary, epoch)

  # (Updated)
  def _define_inputs(self):
    shape = [None]
    shape.extend(self.data_shape)
    self.videos = tf.placeholder(
      tf.float32,
      shape=shape,
      name='input_videos')
    self.labels = tf.placeholder(
      tf.float32,
      shape=[None, self.n_classes],
      name='labels')
    self.learning_rate = tf.placeholder(
      tf.float32,
      shape=[],
      name='learning_rate')
    self.is_training = tf.placeholder(tf.bool, shape=[])

  # (Updated)
  def composite_function(self, _input, out_features, kernel_size=3):
    """Function from paper H_l that performs:
    - batch normalization
    - ReLU nonlinearity
    - convolution with required kernel
    - dropout, if required
    """
    with tf.variable_scope("composite_function"):
      # BN
      output = self.batch_norm(_input)
      # ReLU
      with tf.name_scope("ReLU"):
        output = tf.nn.relu(output)
      # convolution
      output = self.conv3d(
        output, out_features=out_features, kernel_size=kernel_size)
        # dropout(in case of training and in case it is no 1.0)
      output = self.dropout(output)
    return output

  # (Updated)
  def bottleneck(self, _input, out_features):
    with tf.variable_scope("bottleneck"):
      output = self.batch_norm(_input)
      with tf.name_scope("ReLU"):
        output = tf.nn.relu(output)
      inter_features = out_features * 4
      output = self.conv3d(
        output, out_features=inter_features, kernel_size=1,
        padding='VALID')
      output = self.dropout(output)
    return output

  # (Updated)
  def add_internal_layer(self, _input, growth_rate):
    """Perform H_l composite function for the layer and after concatenate
    input with output from composite function.
    """
    # call composite function with 3x3 kernel
    if not self.bc_mode:
      comp_out = self.composite_function(
        _input, out_features=growth_rate, kernel_size=3)
    elif self.bc_mode:
      bottleneck_out = self.bottleneck(_input, out_features=growth_rate)
      comp_out = self.composite_function(
        bottleneck_out, out_features=growth_rate, kernel_size=3)
    # concatenate _input with out from composite function
    with tf.name_scope("concat"):
      if TF_VERSION >= 1.0:
          output = tf.concat(axis=4, values=(_input, comp_out))
      else:
        output = tf.concat(4, (_input, comp_out))
    return output

  # (Updated)
  def add_block(self, _input, growth_rate, layers_per_block):
    """Add N H_l internal layers"""
    output = _input
    for layer in range(layers_per_block):
      with tf.variable_scope("layer_%d" % layer):
        output = self.add_internal_layer(output, growth_rate)
    return output

  # (Updated)
  def transition_layer(self, _input, pool_depth=2):
    """Call H_l composite function with 1x1 kernel and pooling
    """
    # call composite function with 1x1 kernel
    out_features = int(int(_input.get_shape()[-1]) * self.reduction)
    output = self.composite_function(
      _input, out_features=out_features, kernel_size=1)
    # run pooling
    with tf.name_scope("pooling"):
      output = self.pool(output, k=2, d=pool_depth)
    return output

  # (Updated)
  def trainsition_layer_to_classes(self, _input):
    """This is last transition to get probabilities by classes. It perform:
    - batch normalization
    - ReLU nonlinearity
    - wide pooling
    - FC layer multiplication
    """
    # BN
    output = self.batch_norm(_input)
    # ReLU
    with tf.name_scope("ReLU"):
      output = tf.nn.relu(output)
    # pooling
    last_pool_kernel_width = int(output.get_shape()[-2])
    last_pool_kernel_height = int(output.get_shape()[-3])
    last_sequence_length = int(output.get_shape()[1])
    with tf.name_scope("pooling"):
      output = self.pool(output, k = last_pool_kernel_height,
                         d = last_sequence_length,
                         width_k = last_pool_kernel_width,
                         k_stride_width = last_pool_kernel_width)
    # FC
    features_total = int(output.get_shape()[-1])
    output = tf.reshape(output, [-1, features_total])
    W = self.weight_variable_xavier(
      [features_total, self.n_classes], name='W')
    bias = self.bias_variable([self.n_classes])
    logits = tf.matmul(output, W) + bias
    # Local 
    # features_total = int(output.get_shape()[-1])
    # output = tf.reshape(output, [-1, features_total])
    # lc_weight = self.weight_variable_xavier(
    #   [features_total, 100], name='lc_weight')
    # lc_bias = self.bias_variable([100], 'lc_bias')
    # local = tf.nn.relu(tf.matmul(output, lc_weight) + lc_bias)
    # local = self.dropout(local)
    # # Second Local
    # lc2_weight = self.weight_variable_xavier(
    #   [100, 100], name='lc2_weight')
    # lc2_bias = self.bias_variable([100], 'lc2_bias')
    # local2 = tf.nn.relu(tf.matmul(local, lc2_weight) + lc2_bias)
    # local2 = self.dropout(local2)
    # # Classification
    # weight = self.weight_variable_xavier(
    #   [100, self.n_classes], name='weight')
    # bias = self.bias_variable([self.n_classes], 'bias')
    # logits = tf.matmul(local2, weight) + bias
    return logits
  
  # (Updated)
  def conv3d(self, _input, out_features, kernel_size,
         strides=[1, 1, 1, 1, 1], padding='SAME'):
    in_features = int(_input.get_shape()[-1])
    kernel = self.weight_variable_msra(
      [kernel_size, kernel_size, kernel_size, in_features, out_features],
      name='kernel')
    with tf.name_scope("3DConv"):
      output = tf.nn.conv3d(_input, kernel, strides, padding)
    return output

  # (Updated)
  def pool(self, _input, k, d=2, width_k=None, type='avg', k_stride=None, d_stride=None, k_stride_width=None):
    if not width_k: width_k = k
    ksize = [1, d, k, width_k, 1]
    if not k_stride: k_stride = k
    if not k_stride_width: k_stride_width = k_stride
    if not d_stride: d_stride = d
    strides = [1, d_stride, k_stride, k_stride_width, 1]
    padding = 'SAME'
    if type is 'max':
      output = tf.nn.max_pool3d(_input, ksize, strides, padding)
    elif type is 'avg':
      output = tf.nn.avg_pool3d(_input, ksize, strides, padding)
    else:
      output = None
    return output

  # (Updated)
  def batch_norm(self, _input):
    with tf.name_scope("batch_normalization"):
      output = tf.contrib.layers.batch_norm(
        _input, scale=True, is_training=self.is_training,
        updates_collections=None)
    return output

  # (Updated)
  def dropout(self, _input):
    if self.keep_prob < 1:
      with tf.name_scope('dropout'):
        output = tf.cond(
          self.is_training,
          lambda: tf.nn.dropout(_input, self.keep_prob),
          lambda: _input
        )
    else:
      output = _input
    return output

  # (Updated)
  def weight_variable_msra(self, shape, name):
    return tf.get_variable(
      name=name,
      shape=shape,
      initializer=tf.contrib.layers.variance_scaling_initializer())

  # (Updated)
  def weight_variable_xavier(self, shape, name):
    return tf.get_variable(
      name,
      shape=shape,
      initializer=tf.contrib.layers.xavier_initializer())

  # (Updated)
  def bias_variable(self, shape, name='bias'):
    initial = tf.constant(0.0, shape=shape)
    return tf.get_variable(name, initializer=initial)

  # (Updated)
  def _build_graph(self):
    growth_rate = self.growth_rate
    layers_per_block = self.layers_per_block
    # first - initial 3 x 3 x 3 conv to first_output_features
    with tf.variable_scope("Initial_convolution"):
      output = self.conv3d(
        self.videos,
        out_features=self.first_output_features,
        kernel_size=7,
        strides=[1, 1, 2, 2, 1])
      # first pooling
      output = self.pool(output, k=3, d=3, k_stride=2, d_stride=1)

    # add N required blocks
    for block in range(self.total_blocks):
      with tf.variable_scope("Block_%d" % block):
        output = self.add_block(output, growth_rate, layers_per_block)
      # last block exist without transition layer
      if block != self.total_blocks - 1:
        with tf.variable_scope("Transition_after_block_%d" % block):
          #pool_depth = 1 if block == 0 else 2
          pool_depth = 2
          output = self.transition_layer(output, pool_depth)

    with tf.variable_scope("Transition_to_classes"):
      logits = self.trainsition_layer_to_classes(output)
    prediction = tf.nn.softmax(logits)

    # Losses
    cross_entropy = tf.reduce_mean(tf.nn.softmax_cross_entropy_with_logits(
      logits=logits, labels=self.labels))
    self.cross_entropy = cross_entropy
    l2_loss = tf.add_n(
      [tf.nn.l2_loss(var) for var in tf.trainable_variables()])

    # Optimizer and train step
    optimizer = tf.train.MomentumOptimizer(
      self.learning_rate, self.nesterov_momentum, use_nesterov=True)
    self.train_step = optimizer.minimize(
      cross_entropy + l2_loss * self.weight_decay)

    correct_prediction = tf.equal(
      tf.argmax(prediction, 1),
      tf.argmax(self.labels, 1))
    self.accuracy = tf.reduce_mean(tf.cast(correct_prediction, tf.float32))

  # (Updated)
  def train_all_epochs(self, train_params):
    n_epochs           = train_params['n_epochs']
    init_learning_rate = train_params['initial_learning_rate']
    batch_size         = train_params['batch_size']
    reduce_lr_epoch_1  = train_params['reduce_lr_epoch_1']
    reduce_lr_epoch_2  = train_params['reduce_lr_epoch_2']
    total_start_time   = time.time()

    # Restore the model if we have
    start_epoch = self.load_model()
    
    # Start training 
    for epoch in range(start_epoch, n_epochs + 1):
      print("\n", '-' * 30, "Train epoch: %d" % epoch, '-' * 30, '\n')
      start_time = time.time()
      learning_rate = init_learning_rate
      # Update the learning rate according to the decay parameter
      if epoch >= reduce_lr_epoch_1 and epoch < reduce_lr_epoch_2:
        learning_rate = learning_rate / 10
        print("Decrease learning rate, new lr = %f" % learning_rate)
      elif epoch >= reduce_lr_epoch_2:
        learning_rate = learning_rate / 100
        print("Decrease learning rate, new lr = %f" % learning_rate)

      print("Training...")
      loss, acc = self.train_one_epoch(
        self.data_provider.train, batch_size, learning_rate)
      if self.should_save_logs:
        self.log_loss_accuracy(loss, acc, epoch, prefix='train')

      if train_params.get('validation_set', False):
        print("Validation...")
        loss, acc = self.test(
          self.data_provider.validation, batch_size)
        if self.should_save_logs:
          self.log_loss_accuracy(loss, acc, epoch, prefix='valid')

      time_per_epoch = time.time() - start_time
      seconds_left = int((n_epochs - epoch) * time_per_epoch)
      print("Time per epoch: %s, Est. complete in: %s" % (
        str(timedelta(seconds=time_per_epoch)),
        str(timedelta(seconds=seconds_left))))

      if self.should_save_model:
        self.save_model(global_step=epoch)

    total_training_time = time.time() - total_start_time
    print("\nTotal training time: %s" % str(timedelta(
      seconds=total_training_time)))

  # (Updated)
  def train_one_epoch(self, data, batch_size, learning_rate):
    num_examples = data.num_examples
    total_loss = []
    total_accuracy = []
    for i in range(num_examples // batch_size):
      # videos size is (numpy array):
      #   [batch_size, sequence_length, width, height, channels]
      # labels size is (numpy array):
      #   [batch_size, num_classes] 
      videos, labels = data.next_batch(batch_size)
      feed_dict = {
        self.videos: videos,
        self.labels: labels,
        self.learning_rate: learning_rate,
        self.is_training: True,
      }
      fetches = [self.train_step, self.cross_entropy, self.accuracy]
      result = self.sess.run(fetches, feed_dict=feed_dict)
      _, loss, accuracy = result
      total_loss.append(loss)
      total_accuracy.append(accuracy)
      if self.should_save_logs:
        self.batches_step += 1
        self.log_loss_accuracy(
          loss, accuracy, self.batches_step, prefix='per_batch',
          should_print=False)
    mean_loss = np.mean(total_loss)
    mean_accuracy = np.mean(total_accuracy)
    return mean_loss, mean_accuracy

  # (Updated)
  def test(self, data, batch_size):
    num_examples = data.num_examples
    total_loss = []
    total_accuracy = []
    for i in range(num_examples // batch_size):
      batch = data.next_batch(batch_size)
      feed_dict = {
        self.videos: batch[0],
        self.labels: batch[1],
        self.is_training: False,
      }
      fetches = [self.cross_entropy, self.accuracy]
      loss, accuracy = self.sess.run(fetches, feed_dict=feed_dict)
      total_loss.append(loss)
      total_accuracy.append(accuracy)
    mean_loss = np.mean(total_loss)
    mean_accuracy = np.mean(total_accuracy)
    return mean_loss, mean_accuracy