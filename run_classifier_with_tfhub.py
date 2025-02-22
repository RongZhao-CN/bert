# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors.
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
"""BERT finetuning runner with TF-Hub."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import optimization
import run_classifier
import tokenization
import tensorflow as tf
import tensorflow_hub as hub
import tensorflow.compat.v1 as tf1

flags = tf1.flags

FLAGS = flags.FLAGS

flags.DEFINE_string(
    "bert_hub_module_handle", None,
    "Handle for the BERT TF-Hub module.")


def create_model(is_training, input_ids, input_mask, segment_ids, labels,
                 num_labels, bert_hub_module_handle):
  """Creates a classification model."""
  tags = set()
  if is_training:
    tags.add("train")
  bert_module = hub.Module(bert_hub_module_handle, tags=tags, trainable=True)
  bert_inputs = dict(
      input_ids=input_ids,
      input_mask=input_mask,
      segment_ids=segment_ids)
  bert_outputs = bert_module(
      inputs=bert_inputs,
      signature="tokens",
      as_dict=True)

  # In the demo, we are doing a simple classification task on the entire
  # segment.
  #
  # If you want to use the token-level output, use
  # bert_outputs["sequence_output"] instead.
  output_layer = bert_outputs["pooled_output"]

  hidden_size = output_layer.shape[-1].value

  output_weights = tf1.get_variable(
      "output_weights", [num_labels, hidden_size],
      initializer=tf1.truncated_normal_initializer(stddev=0.02))

  output_bias = tf1.get_variable(
      "output_bias", [num_labels], initializer=tf1.zeros_initializer())

  with tf1.variable_scope("loss"):
    if is_training:
      # I.e., 0.1 dropout
      output_layer = tf1.nn.dropout(output_layer, keep_prob=0.9)

    logits = tf1.matmul(output_layer, output_weights, transpose_b=True)
    logits = tf1.nn.bias_add(logits, output_bias)
    probabilities = tf1.nn.softmax(logits, axis=-1)
    log_probs = tf1.nn.log_softmax(logits, axis=-1)

    one_hot_labels = tf1.one_hot(labels, depth=num_labels, dtype=tf1.float32)

    per_example_loss = -tf1.reduce_sum(one_hot_labels * log_probs, axis=-1)
    loss = tf1.reduce_mean(per_example_loss)

    return (loss, per_example_loss, logits, probabilities)


def model_fn_builder(num_labels, learning_rate, num_train_steps,
                     num_warmup_steps, use_tpu, bert_hub_module_handle):
  """Returns `model_fn` closure for TPUEstimator."""

  def model_fn(features, labels, mode, params):  # pylint: disable=unused-argument
    """The `model_fn` for TPUEstimator."""

    tf1.logging.info("*** Features ***")
    for name in sorted(features.keys()):
      tf1.logging.info("  name = %s, shape = %s" % (name, features[name].shape))

    input_ids = features["input_ids"]
    input_mask = features["input_mask"]
    segment_ids = features["segment_ids"]
    label_ids = features["label_ids"]

    is_training = (mode == tf1.estimator.ModeKeys.TRAIN)

    (total_loss, per_example_loss, logits, probabilities) = create_model(
        is_training, input_ids, input_mask, segment_ids, label_ids, num_labels,
        bert_hub_module_handle)

    output_spec = None
    if mode == tf1.estimator.ModeKeys.TRAIN:
      train_op = optimization.create_optimizer(
          total_loss, learning_rate, num_train_steps, num_warmup_steps, use_tpu)

      output_spec = tf1.contrib.tpu.TPUEstimatorSpec(
          mode=mode,
          loss=total_loss,
          train_op=train_op)
    elif mode == tf1.estimator.ModeKeys.EVAL:

      def metric_fn(per_example_loss, label_ids, logits):
        predictions = tf1.argmax(logits, axis=-1, output_type=tf1.int32)
        accuracy = tf1.metrics.accuracy(label_ids, predictions)
        loss = tf1.metrics.mean(per_example_loss)
        return {
            "eval_accuracy": accuracy,
            "eval_loss": loss,
        }

      eval_metrics = (metric_fn, [per_example_loss, label_ids, logits])
      output_spec = tf1.contrib.tpu.TPUEstimatorSpec(
          mode=mode,
          loss=total_loss,
          eval_metrics=eval_metrics)
    elif mode == tf1.estimator.ModeKeys.PREDICT:
      output_spec = tf1.contrib.tpu.TPUEstimatorSpec(
          mode=mode, predictions={"probabilities": probabilities})
    else:
      raise ValueError(
          "Only TRAIN, EVAL and PREDICT modes are supported: %s" % (mode))

    return output_spec

  return model_fn


def create_tokenizer_from_hub_module(bert_hub_module_handle):
  """Get the vocab file and casing info from the Hub module."""
  with tf1.Graph().as_default():
    bert_module = hub.Module(bert_hub_module_handle)
    tokenization_info = bert_module(signature="tokenization_info", as_dict=True)
    with tf1.Session() as sess:
      vocab_file, do_lower_case = sess.run([tokenization_info["vocab_file"],
                                            tokenization_info["do_lower_case"]])
  return tokenization.FullTokenizer(
      vocab_file=vocab_file, do_lower_case=do_lower_case)


def main(_):
  tf1.logging.set_verbosity(tf1.logging.INFO)

  processors = {
      "cola": run_classifier.ColaProcessor,
      "mnli": run_classifier.MnliProcessor,
      "mrpc": run_classifier.MrpcProcessor,
  }

  if not FLAGS.do_train and not FLAGS.do_eval:
    raise ValueError("At least one of `do_train` or `do_eval` must be True.")

  tf1.gfile.MakeDirs(FLAGS.output_dir)

  task_name = FLAGS.task_name.lower()

  if task_name not in processors:
    raise ValueError("Task not found: %s" % (task_name))

  processor = processors[task_name]()

  label_list = processor.get_labels()

  tokenizer = create_tokenizer_from_hub_module(FLAGS.bert_hub_module_handle)

  tpu_cluster_resolver = None
  if FLAGS.use_tpu and FLAGS.tpu_name:
    tpu_cluster_resolver = tf1.contrib.cluster_resolver.TPUClusterResolver(
        FLAGS.tpu_name, zone=FLAGS.tpu_zone, project=FLAGS.gcp_project)

  is_per_host = tf1.contrib.tpu.InputPipelineConfig.PER_HOST_V2
  run_config = tf1.contrib.tpu.RunConfig(
      cluster=tpu_cluster_resolver,
      master=FLAGS.master,
      model_dir=FLAGS.output_dir,
      save_checkpoints_steps=FLAGS.save_checkpoints_steps,
      tpu_config=tf1.contrib.tpu.TPUConfig(
          iterations_per_loop=FLAGS.iterations_per_loop,
          num_shards=FLAGS.num_tpu_cores,
          per_host_input_for_training=is_per_host))

  train_examples = None
  num_train_steps = None
  num_warmup_steps = None
  if FLAGS.do_train:
    train_examples = processor.get_train_examples(FLAGS.data_dir)
    num_train_steps = int(
        len(train_examples) / FLAGS.train_batch_size * FLAGS.num_train_epochs)
    num_warmup_steps = int(num_train_steps * FLAGS.warmup_proportion)

  model_fn = model_fn_builder(
      num_labels=len(label_list),
      learning_rate=FLAGS.learning_rate,
      num_train_steps=num_train_steps,
      num_warmup_steps=num_warmup_steps,
      use_tpu=FLAGS.use_tpu,
      bert_hub_module_handle=FLAGS.bert_hub_module_handle)

  # If TPU is not available, this will fall back to normal Estimator on CPU
  # or GPU.
  estimator = tf1.contrib.tpu.TPUEstimator(
      use_tpu=FLAGS.use_tpu,
      model_fn=model_fn,
      config=run_config,
      train_batch_size=FLAGS.train_batch_size,
      eval_batch_size=FLAGS.eval_batch_size,
      predict_batch_size=FLAGS.predict_batch_size)

  if FLAGS.do_train:
    train_features = run_classifier.convert_examples_to_features(
        train_examples, label_list, FLAGS.max_seq_length, tokenizer)
    tf1.logging.info("***** Running training *****")
    tf1.logging.info("  Num examples = %d", len(train_examples))
    tf1.logging.info("  Batch size = %d", FLAGS.train_batch_size)
    tf1.logging.info("  Num steps = %d", num_train_steps)
    train_input_fn = run_classifier.input_fn_builder(
        features=train_features,
        seq_length=FLAGS.max_seq_length,
        is_training=True,
        drop_remainder=True)
    estimator.train(input_fn=train_input_fn, max_steps=num_train_steps)

  if FLAGS.do_eval:
    eval_examples = processor.get_dev_examples(FLAGS.data_dir)
    eval_features = run_classifier.convert_examples_to_features(
        eval_examples, label_list, FLAGS.max_seq_length, tokenizer)

    tf1.logging.info("***** Running evaluation *****")
    tf1.logging.info("  Num examples = %d", len(eval_examples))
    tf1.logging.info("  Batch size = %d", FLAGS.eval_batch_size)

    # This tells the estimator to run through the entire set.
    eval_steps = None
    # However, if running eval on the TPU, you will need to specify the
    # number of steps.
    if FLAGS.use_tpu:
      # Eval will be slightly WRONG on the TPU because it will truncate
      # the last batch.
      eval_steps = int(len(eval_examples) / FLAGS.eval_batch_size)

    eval_drop_remainder = True if FLAGS.use_tpu else False
    eval_input_fn = run_classifier.input_fn_builder(
        features=eval_features,
        seq_length=FLAGS.max_seq_length,
        is_training=False,
        drop_remainder=eval_drop_remainder)

    result = estimator.evaluate(input_fn=eval_input_fn, steps=eval_steps)

    output_eval_file = os.path.join(FLAGS.output_dir, "eval_results.txt")
    with tf1.io.gfile.GFile(output_eval_file, "w") as writer:
      tf1.logging.info("***** Eval results *****")
      for key in sorted(result.keys()):
        tf1.logging.info("  %s = %s", key, str(result[key]))
        writer.write("%s = %s\n" % (key, str(result[key])))

  if FLAGS.do_predict:
    predict_examples = processor.get_test_examples(FLAGS.data_dir)
    if FLAGS.use_tpu:
      # Discard batch remainder if running on TPU
      n = len(predict_examples)
      predict_examples = predict_examples[:(n - n % FLAGS.predict_batch_size)]

    predict_file = os.path.join(FLAGS.output_dir, "predict.tf_record")
    run_classifier.file_based_convert_examples_to_features(
        predict_examples, label_list, FLAGS.max_seq_length, tokenizer,
        predict_file)

    tf1.logging.info("***** Running prediction*****")
    tf1.logging.info("  Num examples = %d", len(predict_examples))
    tf1.logging.info("  Batch size = %d", FLAGS.predict_batch_size)

    predict_input_fn = run_classifier.file_based_input_fn_builder(
        input_file=predict_file,
        seq_length=FLAGS.max_seq_length,
        is_training=False,
        drop_remainder=FLAGS.use_tpu)

    result = estimator.predict(input_fn=predict_input_fn)

    output_predict_file = os.path.join(FLAGS.output_dir, "test_results.tsv")
    with tf1.io.gfile.GFile(output_predict_file, "w") as writer:
      tf1.logging.info("***** Predict results *****")
      for prediction in result:
        probabilities = prediction["probabilities"]
        output_line = "\t".join(
            str(class_probability)
            for class_probability in probabilities) + "\n"
        writer.write(output_line)


if __name__ == "__main__":
  flags.mark_flag_as_required("data_dir")
  flags.mark_flag_as_required("task_name")
  flags.mark_flag_as_required("bert_hub_module_handle")
  flags.mark_flag_as_required("output_dir")
  tf1.app.run()
