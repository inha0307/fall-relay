"""Small mask-aware lifting model and joint-wise masked losses.

Training arrays: x[N,36], y[N,12,3] in meters, mask[N,12].
The hip-center constraint is shared by training and inference.
"""

import tensorflow as tf


def create_lpn(width=128, batch_size=None):
    layers = tf.keras.layers
    inputs = layers.Input(shape=(36,), batch_size=batch_size, name='xy24_mask12')
    x = layers.Dense(width, name='input_dense')(inputs)
    x = layers.BatchNormalization(name='input_bn')(x)
    x = layers.ReLU()(x)
    for block in range(2):
        shortcut = x
        x = layers.Dense(width, name=f'block{block}_dense0')(x)
        x = layers.BatchNormalization()(x)
        x = layers.ReLU()(x)
        x = layers.Dropout(0.1)(x)
        x = layers.Dense(width, name=f'block{block}_dense1')(x)
        x = layers.BatchNormalization()(x)
        x = layers.Add()([shortcut, x])
        x = layers.ReLU()(x)
    xyz = layers.Reshape((12, 3))(layers.Dense(36, name='xyz36')(x))
    xyz = layers.Lambda(
        lambda v: v - tf.reduce_mean(v[:, 6:8, :], axis=1, keepdims=True), name='recenter_predicted_hips'
    )(xyz)
    return tf.keras.Model(inputs, xyz, name=f'masked_lpn_width{width}')


def masked_coordinate_loss(target, prediction, mask):
    """MSE over supervised xyz; returns(loss, valid joints).

    Empty supervision yields loss0, but caller MUST skip optimizer update,
    including weight decay/BN updates, when valid_joint_count is0.
    """
    selected = tf.cast(mask, tf.bool)
    truth = tf.boolean_mask(target, selected)
    pred = tf.boolean_mask(prediction, selected)
    tf.debugging.assert_all_finite(truth, 'Nonfinite supervised target')
    tf.debugging.assert_all_finite(pred, 'Nonfinite supervised prediction')
    joints = tf.shape(truth)[0]
    loss = tf.math.divide_no_nan(tf.reduce_sum(tf.square(pred - truth)), tf.cast(joints * 3, tf.float32))
    return loss, joints


def masked_mpjpe_meters(target, prediction, mask):
    selected = tf.cast(mask, tf.bool)
    truth = tf.boolean_mask(target, selected)
    pred = tf.boolean_mask(prediction, selected)
    tf.debugging.assert_all_finite(truth, 'Nonfinite supervised target')
    tf.debugging.assert_all_finite(pred, 'Nonfinite supervised prediction')
    errors = tf.norm(pred - truth, axis=-1)
    return tf.math.divide_no_nan(tf.reduce_sum(errors), tf.cast(tf.size(errors), tf.float32)), tf.size(errors)
