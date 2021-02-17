# Lint as: python3
# Copyright 2020 The TensorFlow Authors. All Rights Reserved.
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
"""Tests for conformer layers as in https://arxiv.org/abs/2005.08100."""
# Lint as: PY3
from unittest import mock
from absl.testing import flagsaver
from absl.testing import parameterized

from lingvo import compat as tf
from lingvo.core import bn_layers
from lingvo.core import cluster_factory
from lingvo.core import conformer_layer
from lingvo.core import gshard_builder
from lingvo.core import layers
from lingvo.core import py_utils
from lingvo.core import test_utils

import numpy as np


class LConvLayerTest(test_utils.TestCase, parameterized.TestCase):

  @parameterized.named_parameters(
      ('BN',),
      ('GN', 'gn'),
  )
  def testBasic(self, norm='bn'):
    batch, seqlen, dim = 2, 16, 4
    inputs = tf.zeros([batch, seqlen, dim])
    paddings = tf.zeros([batch, seqlen])

    p = conformer_layer.LConvLayer.CommonParams(input_dim=dim, kernel_size=3)
    p.name = 'lconv_layer'
    if norm == 'gn':
      # default is bn
      p.conv_norm_layer_tpl = (
          bn_layers.GroupNormLayer.Params().Set(num_groups=2))
    elif norm != 'bn':
      raise ValueError('Only gn and bn are supported.')

    l = p.Instantiate()
    outputs = l.FPropDefaultTheta(inputs, paddings)

    with self.session() as sess:
      tf.global_variables_initializer().run()
      out_vals = sess.run(outputs)
      print([x.shape for x in out_vals])

  @parameterized.named_parameters(
      ('Basic',),
      ('BasicGN', False, 'gn'),
      ('SkipNorm', True),
  )
  def testStreamStep(self, testonly_skip_norm_layers=False, norm_type='ln'):
    with flagsaver.flagsaver(testonly_skip_norm_layers=testonly_skip_norm_layers
                            ), cluster_factory.SetEval(True):
      assert norm_type in ('ln', 'gn')
      batch, max_seqlen, input_dim, kernel = 2, 8, 2, 3
      p = conformer_layer.LConvLayer.CommonParams(
          input_dim=input_dim, is_causal=True, kernel_size=kernel)
      if norm_type == 'ln':
        p.conv_norm_layer_tpl = layers.LayerNorm.Params()
      else:
        p.conv_norm_layer_tpl = bn_layers.GroupNormLayer.Params().Set(
            num_groups=2, cumulative=True)
      p.name = 'lconv'

      l = p.Instantiate()
      init_op = tf.global_variables_initializer()

      np.random.seed(None)
      inputs = np.random.normal(
          0.1, 0.5, [batch, max_seqlen, input_dim]).astype(np.float32)
      print(f'np.sum(inputs): {np.sum(inputs)}')
      inputs = tf.convert_to_tensor(inputs)

      seqlen = np.random.randint(
          low=1, high=max_seqlen + 1, size=(batch,), dtype=np.int32)
      print(repr(seqlen))
      seqlen = tf.convert_to_tensor(seqlen)
      paddings = py_utils.PaddingsFromLengths(seqlen, max_seqlen)
      base_outputs, _ = l.FProp(l.theta, inputs, paddings)
      base_outputs *= tf.expand_dims(1. - paddings, -1)

      outputs = []
      state = l.zero_state(batch)
      for i in range(max_seqlen):
        output, _, state = l.StreamStep(l.theta, inputs[:, i:(i + 1), :],
                                        paddings[:, i:(i + 1)], state)
        outputs.append(output)
      # [b, t, d]
      outputs = tf.concat(outputs, axis=1)
      outputs *= tf.expand_dims(1. - paddings, -1)

      with self.session(use_gpu=False) as sess:
        sess.run(init_op)
        expected, actual = sess.run([base_outputs, outputs])
        print(repr(expected))
        print(repr(actual))
        print(f'np.sum(np.abs(expected)): {np.sum(np.abs(expected))}')
        print(f'np.sum(np.abs(actual)): {np.sum(np.abs(actual))}')
        self.assertAllClose(expected, actual)


class ConformerLayerTest(test_utils.TestCase, parameterized.TestCase):

  def setUp(self):
    super().setUp()
    self.batch = 2
    self.maxlen = 32
    self.dim = 4
    self.heads = 2
    self.context = 2

  def _GetCommonParamsKwargs(self):
    return dict(
        input_dim=self.dim,
        atten_num_heads=self.heads,
        atten_left_context=self.context + 1,
        atten_right_context=self.context,
        kernel_size=3,
        fflayer_hidden_dim=4 * self.dim)

  def _GetParams(self):
    kwargs = self._GetCommonParamsKwargs()
    p = conformer_layer.ConformerLayer.CommonParams(**kwargs)
    p.name = 'conformer_layer'
    return p

  def _GetInputs(self, dtype=tf.float32):
    inputs = np.random.rand(self.batch, self.maxlen,
                            self.dim).astype(np.float32)
    paddings = np.zeros((self.batch, self.maxlen), np.float32)

    seqlen = np.random.randint(0, self.maxlen, size=(self.batch,))
    for i in range(self.batch):
      for j in range(self.maxlen):
        paddings[i][j] = 1. if j >= seqlen[i] else 0.
    return tf.constant(inputs, dtype=dtype), tf.constant(paddings, dtype=dtype)

  def _GetGrad(self, l, inputs, paddings):
    in_nmap = py_utils.NestedMap(features=inputs, paddings=paddings)
    out_nmap = l.FPropDefaultTheta(in_nmap)
    loss = tf.reduce_sum(out_nmap.features)
    grads = tf.gradients(
        loss,
        l.vars.Flatten(),
        unconnected_gradients=tf.UnconnectedGradients.ZERO)
    return out_nmap.features, grads

  @parameterized.named_parameters(
      ('Base',),
      ('Reordered', 'conv_before_mhsa'),
  )
  def testBasic(self, layer_order='mhsa_before_conv'):

    p = self._GetParams()
    p.layer_order = layer_order

    l = p.Instantiate()
    inputs, paddings = self._GetInputs()
    outputs, grads = self._GetGrad(l, inputs, paddings)

    with self.session() as sess:
      tf.global_variables_initializer().run()
      out_vals = sess.run(outputs)
      grad_vals = sess.run(grads)
      print([x.shape for x in out_vals])
      print([g.shape for g in grad_vals])

  @parameterized.named_parameters(
      ('F32FPropF32Input', tf.float32, tf.float32),
      ('F32FPropBF16Input', tf.float32, tf.bfloat16),
      ('BF16FPropF32Input', tf.bfloat16, tf.float32),
      ('BF16FPropBF16Input', tf.bfloat16, tf.bfloat16),
  )
  def testFPropDtypes(self, fprop_dtype, input_dtype):
    p = self._GetParams()
    # batch_norm does not support bfloat16 on CPU.
    p.lconv_tpl.conv_norm_layer_tpl = (
        bn_layers.GroupNormLayer.Params().Set(num_groups=2))
    p.cls.SetFPropDtype(p, fprop_dtype)

    l = p.Instantiate()
    inputs, paddings = self._GetInputs(dtype=input_dtype)
    outputs, grads = self._GetGrad(l, inputs, paddings)

    with self.session() as sess:
      tf.global_variables_initializer().run()
      out_vals = sess.run(outputs)
      grad_vals = sess.run(grads)
      print([x.shape for x in out_vals])
      print([g.shape for g in grad_vals])

  @parameterized.named_parameters(
      ('Start', True, False),
      ('End', False, True),
      ('StartAndEnd', True, True),
      ('None', False, False),
  )
  def testMoEFFLayerClassMethodInitParity(self, use_fflayer_start_moe,
                                          use_fflayer_end_moe):
    """Tests Conformer-MoE initializations via classmethods and explicitly."""

    num_experts, num_groups, num_devices, per_expert_capacity_dim = 2, 2, 2, 2
    # Create params setting MoEBuilder params explicitly.
    ref_p = self._GetParams()
    if use_fflayer_start_moe:
      # Set MoEBuilder params explicitly.
      ref_p.fflayer_start_tpl = gshard_builder.MoEBuilder.Params().Set(
          e_dim=num_experts,
          c_dim=per_expert_capacity_dim,
          num_devices=num_devices,
          num_groups=num_groups)
    if use_fflayer_end_moe:
      ref_p.fflayer_end_tpl = gshard_builder.MoEBuilder.Params().Set(
          e_dim=num_experts,
          c_dim=per_expert_capacity_dim,
          num_devices=num_devices,
          num_groups=num_groups)

    # Params setting MoEBuilder params via classmethod.
    moe_p = self._GetParams()
    if use_fflayer_start_moe:
      # Set MoEBuilder params via classmethod.
      moe_p.cls.SetMoEFFLayerStartParams(moe_p, num_devices, num_groups,
                                         num_experts, per_expert_capacity_dim)
    if use_fflayer_end_moe:
      moe_p.cls.SetMoEFFLayerEndParams(moe_p, num_devices, num_groups,
                                       num_experts, per_expert_capacity_dim)
    # Verify layer params are equal in both cases.
    with self.subTest('testParamsParity'):
      self.assertEqual(ref_p, moe_p)

    # Test both initializations and verify moe sublayer.
    with self.subTest('testInit'):
      ref_p.name = 'ref_moe_conformer_layer'
      ref_layer = ref_p.Instantiate()
      moe_p.name = 'classmethod_moe_conformer_layer'
      moe_layer = moe_p.Instantiate()
      for layer in (ref_layer, moe_layer):
        if use_fflayer_start_moe:
          self.assertNotIn('fflayer_start', layer.children)
          self.assertIn('fflayer_start_moe', layer.children)
        if use_fflayer_end_moe:
          self.assertNotIn('fflayer_end', layer.children)
          self.assertIn('fflayer_end_moe', layer.children)

  @parameterized.named_parameters(
      ('Start', True, False, 0.593693),
      ('End', False, True, 0.4582923),
      ('StartAndEnd', True, True, 1.0213419),
      ('None', False, False, 0.0),
  )
  def testMoEFFLayerFProp(self, use_fflayer_start_moe, use_fflayer_end_moe,
                          expected_aux_loss):
    p = self._GetParams()
    if use_fflayer_start_moe:
      p.fflayer_start_tpl = gshard_builder.MoEBuilder.Params().Set(
          e_dim=2, c_dim=2, num_devices=2)
    if use_fflayer_end_moe:
      p.fflayer_end_tpl = gshard_builder.MoEBuilder.Params().Set(
          e_dim=2, c_dim=2, num_devices=2)
    l = p.Instantiate()
    inputs, paddings = self._GetInputs()
    inputs = tf.convert_to_tensor(inputs)
    paddings = tf.convert_to_tensor(paddings)
    in_nmap = py_utils.NestedMap(features=inputs, paddings=paddings)
    in_nmap.aux_loss = tf.convert_to_tensor(0., py_utils.FPropDtype(p))
    out_nmap = l.FPropDefaultTheta(in_nmap)
    self.assertIn('aux_loss', out_nmap)
    loss = tf.reduce_sum(out_nmap.features) + 0.01 * out_nmap.aux_loss
    grads = tf.gradients(
        loss,
        l.vars.Flatten(),
        unconnected_gradients=tf.UnconnectedGradients.ZERO)

    with self.session() as sess:
      tf.global_variables_initializer().run()
      out_vals = sess.run(out_nmap.features)
      grad_vals = sess.run(grads)
      self.assertEqual(out_nmap.aux_loss.shape, ())
      aux_loss = sess.run(out_nmap.aux_loss)
      self.assertAlmostEqual(expected_aux_loss, aux_loss, places=5)
      print([x.shape for x in out_vals])
      print([g.shape for g in grad_vals])

  def testRemat(self):
    inputs, paddings = self._GetInputs()
    base_p = self._GetParams()
    base_p.name = 'base'
    base_p.layer_order = 'conv_before_mhsa'

    new_p = base_p.Copy()
    new_p.name = 'new'
    new_p.remat = True

    base_l = base_p.Instantiate()
    new_l = new_p.Instantiate()

    _, base_grads = self._GetGrad(base_l, inputs, paddings)
    base_grads = base_l.vars.Pack(base_grads)

    _, new_grads = self._GetGrad(new_l, inputs, paddings)
    new_grads = new_l.vars.Pack(new_grads)

    assign_op = [
        tf.assign(dst, src)
        for (src, dst) in zip(base_l.vars.Flatten(), new_l.vars.Flatten())
    ]
    init_op = tf.global_variables_initializer()
    with self.session() as sess:
      sess.run(init_op)
      sess.run(assign_op)
      base_grads_val = sess.run(base_grads)
      new_grads_val = sess.run(new_grads)

      for (k, v1), (_, v2) in zip(base_grads_val.FlattenItems(),
                                  new_grads_val.FlattenItems()):
        self.assertAllClose(v1, v2, msg=k)

  @parameterized.named_parameters(
      ('Basic',),
      ('NegativeLocalContext', -1),
      ('NegativeLeftContext', None, -1, None),
      ('NegativeRightContext', None, None, -1),
      ('NegativeContext1', -1, None, -1),
      ('NegativeContext2', None, -1, -1),
      ('NegativeContext3', -1, -1, None),
      ('NegativeContext4', -1, None, -1),
      ('NegativeContext5', -1, -1, -1),
      ('NegativeContext6', None, None, None),
  )
  def testAttenContextParams(self,
                             local_context=None,
                             left_context=None,
                             right_context=None):
    """Tests atten context cfg params."""
    inputs, paddings = self._GetInputs()
    base_p_kwargs = self._GetCommonParamsKwargs()
    base_p_kwargs['atten_local_context'] = None
    base_p_kwargs['atten_left_context'] = None
    base_p_kwargs['atten_right_context'] = None
    base_p = conformer_layer.ConformerLayer.CommonParams(**base_p_kwargs)
    base_p.name = 'base'
    base_p.layer_order = 'conv_before_mhsa'

    new_p_kwargs = self._GetCommonParamsKwargs()
    new_p_kwargs['atten_local_context'] = local_context
    new_p_kwargs['atten_left_context'] = left_context
    new_p_kwargs['atten_right_context'] = right_context
    new_p = conformer_layer.ConformerLayer.CommonParams(**new_p_kwargs)
    new_p.name = 'new'
    new_p.layer_order = 'conv_before_mhsa'

    base_l = base_p.Instantiate()
    new_l = new_p.Instantiate()

    _, base_grads = self._GetGrad(base_l, inputs, paddings)
    base_grads = base_l.vars.Pack(base_grads)

    _, new_grads = self._GetGrad(new_l, inputs, paddings)
    new_grads = new_l.vars.Pack(new_grads)

    assign_op = [
        tf.assign(dst, src)
        for (src, dst) in zip(base_l.vars.Flatten(), new_l.vars.Flatten())
    ]
    init_op = tf.global_variables_initializer()
    with self.session() as sess:
      sess.run(init_op)
      sess.run(assign_op)
      base_grads_val = sess.run(base_grads)
      new_grads_val = sess.run(new_grads)

      for (k, v1), (_, v2) in zip(base_grads_val.FlattenItems(),
                                  new_grads_val.FlattenItems()):
        self.assertAllClose(v1, v2, msg=k)

  @parameterized.named_parameters(
      ('Basic', 8, 'SWISH'),
      ('BasicReLU', 16, 'RELU'),
  )
  def testFFlayerParams(self, fflayer_hidden_dim=None, fflayer_activation=None):

    p = self._GetParams()
    p.fflayer_hidden_dim = fflayer_hidden_dim
    p.fflayer_activation = fflayer_activation
    layer = p.Instantiate()

    start_fflayer = layer.fflayer_start
    actual_start_hidden_dim = start_fflayer.params.hidden_dim
    actual_start_activation = start_fflayer.params.activation
    end_fflayer = layer.fflayer_end
    actual_end_hidden_dim = end_fflayer.params.hidden_dim
    actual_end_activation = end_fflayer.params.activation

    self.assertEqual(fflayer_hidden_dim, actual_start_hidden_dim)
    self.assertEqual(fflayer_activation, actual_start_activation)
    self.assertEqual(fflayer_hidden_dim, actual_end_hidden_dim)
    self.assertEqual(fflayer_activation, actual_end_activation)

  def testCommonParamsAbuse(self):
    """Checks CommonParams() is not called in __init__()."""
    p = self._GetParams()
    with mock.patch(
        'lingvo.core.conformer_layer.ConformerLayer.CommonParams',
        autospec=True) as m1:
      with mock.patch(
          'lingvo.core.conformer_layer.LConvLayer.CommonParams',
          autospec=True) as m2:
        p.Instantiate()
        self.assertFalse(m1.called)
        self.assertFalse(m2.called)

  @parameterized.named_parameters(
      ('WithoutRelPosAtten', False),
      ('WithRelPosAtten', True),
  )
  def testApplyGShard(self, use_relative_atten):
    with self.session() as sess:
      conformer_p = conformer_layer.ConformerLayer.CommonParams(
          input_dim=self.dim,
          atten_num_heads=self.heads,
          atten_local_context=self.context,
          use_relative_atten=use_relative_atten,
          kernel_size=2,
          fflayer_hidden_dim=4 * self.dim)
      conformer_p.name = 'conformer_layer'
      conformer_layer.ApplyGshard(
          conformer_p,
          device_mesh=[1, 2],
          proj_w_split_list=[[0, 1], [1, 0]],
          proj_activation_split_list=[[0, -1, 1], [0, -1, -1]],
          atten_dnh_w_split=[0, 1, -1],
          atten_blnh_activation_split=[0, -1, 1, -1],
          atten_bld_activation_split=[0, -1, -1],
          lconv_df_w_split=[0, 1],
          lconv_hwim_w_split=[-1, -1, 1, -1],
          lconv_fd_w_split=[-1, -1],
          lconv_blf_activation_split=[0, -1, 1],
          lconv_bld_activation_split=[0, -1, -1])
      inputs, paddings = self._GetInputs()
      conformer_l = conformer_p.Instantiate()
      outputs = conformer_l.FProp(
          conformer_l.theta,
          py_utils.NestedMap(
              features=tf.convert_to_tensor(inputs),
              paddings=tf.convert_to_tensor(paddings)))
      tf.logging.info('outputs=%s', outputs)
      tf.global_variables_initializer().run()
      out_vals = sess.run(outputs)
      print([x.shape for x in out_vals.Flatten()])

  @parameterized.named_parameters(
      ('Dropout', 'dropout_prob', 0.1),
      ('LayerOrder', 'layer_order', 'conv_before_mhsa'),
      ('FFLayerActivation', 'fflayer_activation', 'GELU'),
      ('UseRelativeAttention', 'use_relative_atten', False),
      ('IsCausal', 'is_causal', True))
  def testCommonParamsSet(self, param_name, param_val):
    """Checks values set in CommonParams() correctly."""

    def _GetMinimalCommonParamsKwargs():
      """These args are required to be set to call CommonParams."""
      return dict(
          input_dim=2, atten_num_heads=4, kernel_size=3, fflayer_hidden_dim=8)

    kwargs = _GetMinimalCommonParamsKwargs()
    kwargs.update({param_name: param_val})
    p = conformer_layer.ConformerLayer.CommonParams(**kwargs)
    p.name = 'conformer_layer'
    self.assertEqual(p.Get(param_name), param_val)

  @parameterized.named_parameters(
      ('Basic',),
      ('BasicGN', False, 'gn'),
      ('BasicGNG1', False, 'gn', 1),
      ('BasicGNG8', False, 'gn', 8),
      ('SkipNorm', True),
      ('SkipNormGN', True),
  )
  def testStreamStep(self,
                     testonly_skip_norm_layers=False,
                     norm_type='ln',
                     num_groups=2):
    assert norm_type in ('ln', 'gn'), norm_type
    with flagsaver.flagsaver(testonly_skip_norm_layers=testonly_skip_norm_layers
                            ), cluster_factory.SetEval(True):
      batch, max_seqlen, input_dim, kernel = 2, 16, 8, 3
      num_heads, left_context, ffn_dim = 2, 3, 4
      p = conformer_layer.ConformerLayer.CommonParams(
          input_dim=input_dim,
          is_causal=True,
          atten_num_heads=num_heads,
          atten_left_context=left_context,
          atten_right_context=0,
          use_relative_atten=False,
          fflayer_hidden_dim=ffn_dim,
          kernel_size=kernel,
          layer_order='conv_before_mhsa')
      if norm_type == 'ln':
        p.lconv_tpl.conv_norm_layer_tpl = layers.LayerNorm.Params()
      else:
        p.lconv_tpl.conv_norm_layer_tpl = bn_layers.GroupNormLayer.Params().Set(
            num_groups=num_groups, cumulative=True)
      p.name = 'conformer'

      l = p.Instantiate()
      init_op = tf.global_variables_initializer()

      np.random.seed(None)
      inputs = 5 * np.random.normal(
          0.1, 0.5, [batch, max_seqlen, input_dim]).astype(np.float32)
      print(f'np.sum(inputs): {np.sum(inputs)}')
      inputs = tf.convert_to_tensor(inputs)

      seqlen = np.random.randint(
          low=1, high=max_seqlen + 1, size=(batch,), dtype=np.int32)
      print(repr(seqlen))
      seqlen = tf.convert_to_tensor(seqlen)
      paddings = py_utils.PaddingsFromLengths(seqlen, max_seqlen)

      base_output_map = l.FProp(
          l.theta, py_utils.NestedMap(features=inputs, paddings=paddings))
      base_outputs = base_output_map.features
      base_outputs *= tf.expand_dims(1. - paddings, -1)

      outputs = []
      state = l.zero_state(batch)
      for i in range(max_seqlen):
        output, _, state = l.StreamStep(l.theta, inputs[:, i:(i + 1), :],
                                        paddings[:, i:(i + 1)], state)
        outputs.append(output)
      # [b, t, d]
      outputs = tf.concat(outputs, axis=1)
      outputs *= tf.expand_dims(1. - paddings, -1)

      with self.session(use_gpu=False) as sess:
        sess.run(init_op)
        expected, actual = sess.run([base_outputs, outputs])
        print(repr(expected))
        print(repr(actual))
        print(f'np.sum(np.abs(expected)): {np.sum(np.abs(expected))}')
        print(f'np.sum(np.abs(actual)): {np.sum(np.abs(actual))}')
        tol = 2.e-6 if testonly_skip_norm_layers else 2.e-5
        self.assertAllClose(expected, actual, atol=tol, rtol=tol)


if __name__ == '__main__':
  tf.test.main()
