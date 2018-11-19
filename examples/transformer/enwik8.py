#!/usr/bin/env python

'''
Example of the blocksparse transformer on enwik8.

To download data:

wget http://mattmahoney.net/dc/enwik8.zip
unzip enwik8.zip -d /tmp
'''

import argparse
import numpy as np
import tensorflow as tf
from mpi4py import MPI
from blocksparse.transformer import BlocksparseTransformer, softmax_cross_entropy
from blocksparse.optimize    import AdamOptimizer, AdafactorOptimizer, ClipGlobalNorm
from blocksparse.norms       import layer_norm
from blocksparse.embed       import embedding_lookup
from blocksparse.ewops       import bias_relu, float_cast, scale_tensor
from blocksparse.nccl        import allreduce, group_allreduce, sync_variables_op
from blocksparse.quantize    import log_stats


def layernorm(x, scope, epsilon=1e-5, relu=False):
    """
    normalize state vector to be zero mean / unit variance + learned scale/shift
    """
    n_state = x.shape[-1].value
    with tf.variable_scope(scope):
        gain = tf.get_variable('gain', [n_state], initializer=tf.constant_initializer(1.0))
        bias = tf.get_variable('bias', [n_state], initializer=tf.constant_initializer(0.0))
        return layer_norm(x, gain, bias, axis=-1, epsilon=epsilon, relu=relu)


def conv1d(x, scope, nf, relu=False):
    with tf.variable_scope(scope):
        nx    = x.shape[-1].value
        ndims = x.shape.ndims

        w = tf.get_variable("w", [nx, nf], initializer=tf.random_normal_initializer(stddev=0.02))
        b = tf.get_variable("b", [    nf], initializer=tf.constant_initializer(0.0))

        # merge context and batch dims for more efficient matmul
        if ndims > 2:
            y_shape = tf.concat([tf.shape(x)[: ndims - 1], [nf]], axis=0)
            x = tf.reshape(x, [-1, nx])

        # avoid atomics in bias grad, but be careful as tf handles temp memory badly in the presense of async ops like all-reduce
        y = bias_relu(tf.matmul(x, fp16(w)), b, relu=relu, atomics=False)

        if ndims > 2:
            y = tf.reshape(y, y_shape)

        return y

# Fine sparse structure
# Within each block this mask is applied to force the softmax output to zero where the mask is zero
# This is defined as a callback to avoid having to instantiate the full mask in memory at one time.
# The callback value is immediately converted to a bit mask internally.
def causal_subblock_mask(blk_shape, head_idx, query_idx, key_idx, blk_idx):
    """Prohibit positions in sub-blocks from attending to indices in the future.
    Note: query_idx and key_idx are absolute indices rather than relative to
    each block.
    """
    mask = np.ones(blk_shape, dtype=np.bool)
    if query_idx == key_idx:
        for q, k in np.ndindex(blk_shape):
            if k > q:
                mask[q, k] = 0
    return mask

# Coarse sparse structure
# Only layout==1 blocks are computed and materialized in memory
# Block sizes of 8, 16, 32 and 64 are supported (64 being most appropriate for dense attention)
def get_blocksparse_attention_ops(n_timesteps, n_heads):
    blocksize = 64
    n_time_blocks = n_timesteps // blocksize
    layout = np.ones([n_time_blocks, n_time_blocks], dtype=np.bool)
    # No query blocks may attend to key blocks in the future.
    # Much more elaborate structures can be defined here aside from the usual lower triangular.
    for q_idx, k_idx in np.ndindex(n_time_blocks, n_time_blocks):
        if k_idx > q_idx:
            layout[q_idx, k_idx] = 0
    bst = BlocksparseTransformer(layout, block_size=blocksize, mask_callback=causal_subblock_mask, heads=n_heads)
    return bst


def fp16(x):
    # no need to cast the gradients back to fp32 as the all-reduce and optimizers handle fp16/fp32 mixed precision
    return float_cast(x, dtype=tf.float16, dx_dtype=tf.float16)


def fp32(x):
    return float_cast(x, dtype=tf.float32)


def attention(x, scope, n_head, n_timesteps):
    """
    perform multi-head qkv dot-product attention and linear project result
    """
    n_state = x.shape[-1].value
    with tf.variable_scope(scope):
        queries = conv1d(x, 'q', n_state)
        keys    = conv1d(x, 'k', n_state)
        values  = conv1d(x, 'v', n_state)
        # note that split/merge heads is fused into attention ops (no resahpe/transpose needed)

        bst = get_blocksparse_attention_ops(n_timesteps, n_head)
        attention_energies = bst.query_key_op(queries, keys)
        attention_weights  = bst.masked_softmax(attention_energies, scale=tf.rsqrt(n_state / n_head))
        weighted_values    = bst.weight_value_op(attention_weights, values)

        result = conv1d(weighted_values, 'proj', n_state)
        return result


def mlp(x, scope, ratio=4):
    """
    2 layer relu residual mlp with wider first layer
    """
    n_state = x.shape[-1].value
    with tf.variable_scope(scope):
        hidden   = conv1d(x,        'hidden', n_state * ratio, relu=True)  # relu fc layer
        residual = conv1d(hidden, 'residual', n_state)  # project back to state size
        return x + residual


def transformer_block(x, scope, n_head, n_timesteps):
    """
    core component of transformer
    performs attention + residual mlp + layer normalization
    """
    with tf.variable_scope(scope):
        a = attention(x, 'attention', n_head, n_timesteps)
        a = layernorm(a + x, 'norm_a')
        m = mlp(a, 'mlp')
        m = layernorm(m + a, 'norm_m')
        return m


def model(xs, ys, cost_scale, grad_scale):

    with tf.variable_scope("model"):

        with tf.device("/cpu:0"):
            global_step   = tf.Variable(1.0, trainable=False)
            learning_rate = tf.minimum(global_step * tf.constant(1.0/hps.warmup_iters), tf.constant(1.0)) * tf.constant(hps.lr)

        with tf.device("/gpu:0"):

            # Contains scope/var_name substrings we use to group gradients for all reduce
            # You'll want to find groupings that are scheduled uniquely by tensorflow, otherwise allreduce could hang.
            # The groups should be ordered in which the all-reduce is called.
            # Any gradients not matching the substrings will get appended to the last group.
            grad_groups = []

            # embed discrete inputs to continous space and add learned position embeddings
            with tf.variable_scope('embed'):
                x_embed   = fp16(tf.get_variable("x",   [   hps.n_vocab,     hps.n_state], initializer=tf.random_normal_initializer(stddev=0.02)))
                pos_embed = fp16(tf.get_variable('pos', [1, hps.n_timesteps, hps.n_state], initializer=tf.random_normal_initializer(stddev=0.01)))
                h = embedding_lookup(x_embed, xs) + pos_embed
                grad_groups.insert(0, 'embed')

            for l in range(hps.n_layer):
                layer_name = 'layer_%d' % l
                h = transformer_block(h, layer_name, hps.n_head, hps.n_timesteps)
                grad_groups.insert(0, layer_name)

            #average pool transformer features and apply linear classifier
            with tf.variable_scope('logits'):
                h = tf.reshape(h, [-1, hps.n_state])
                logits = tf.matmul(h, x_embed, transpose_b=True)


            # labels = tf.reshape(ys, [-1])
            # loss   = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=fp32(logits), labels=tf.cast(labels, tf.int32))
            loss   = softmax_cross_entropy(logits=logits, labels=ys)
            loss   = tf.reduce_mean(loss)

            params = tf.trainable_variables()
            # use scale_tensor so we can keep the cost_scale a host side placeholder
            grads  = tf.gradients(scale_tensor(loss, cost_scale), params)

            if mpi_size > 1:
                loss = allreduce(loss) * tf.constant(1.0 / mpi_size)

                group_allreduce(grads, params, search_strings=grad_groups)

            global_norm, norm_scale = ClipGlobalNorm(grads, grad_scale=grad_scale, clip_norm=hps.clip_norm)

            # for tuning fp16 cost scaling
            if hps.log_stats and mpi_rank == 0:
                for i, (grad, param) in enumerate(zip(grads, params)):
                    name = param.op.name + "_" + "_".join(str(x) for x in param.shape.as_list())
                    grads[i] = log_stats(grad, tf.cast(global_step, tf.int32), logfile="scale_stats.txt", name=name)


            # use adafactor for most params and adam for embeddings
            fact_grads = list()
            adam_grads = list()
            for grad, param in zip(grads, params):
                if "embed" in param.op.name:
                    # for input embedding, only update param + running stats when embedding vector was selected by input
                    # more stable learning for rarely used embedding entries
                    # Note that we use the x_embed as the output logits projection, so there's little value to using lazy here.
                    # if "x" in param.op.name:
                    #     grad.lazy = True
                    adam_grads.append( (grad, param) )
                else:
                    fact_grads.append( (grad, param) )

            fact = AdafactorOptimizer(learning_rate=learning_rate, norm_scale=norm_scale, grad_scale=grad_scale)
            adam = AdamOptimizer(     learning_rate=learning_rate, norm_scale=norm_scale, grad_scale=grad_scale)
            train_op = tf.group(fact.apply_gradients(fact_grads), adam.apply_gradients(adam_grads))

        # update global step after we're done using it for this update
        with tf.control_dependencies([ train_op ]), tf.device("/cpu:0"):
            update_op = tf.assign_add(global_step, 1.0)

        return loss, tf.group(train_op, update_op), global_norm, norm_scale


def enwik8(path, n_train=int(90e6), n_valid=int(5e6), n_test=int(5e6)):
    X = np.fromstring(open(path).read(n_train + n_valid + n_test), dtype=np.uint8)
    trX, vaX, teX = np.split(X, [n_train, n_train + n_valid])
    return trX, vaX, teX


def iter_data(X, n_timesteps, n_batch, mpi_rank, mpi_size):
    offset = np.random.randint(0, n_timesteps)
    idxs   = np.random.permutation(np.arange(offset, X.size - (n_timesteps + 1), n_timesteps))
    # Truncate the training set this epoch if it does not divide evenly
    sequences_per_batch = mpi_size * n_batch
    length = (idxs.size // sequences_per_batch) * sequences_per_batch
    if length != idxs.size:
        print_rank0('Not including {} sequences'.format(idxs.size - length))
    idxs = idxs[:length]
    # Reshape starting indices to K*mpi_size*n_batch
    idxs = idxs.reshape([-1, mpi_size, n_batch])
    print_rank0(f'Number of minibatches this epoch: {len(idxs)}')
    for minibatch_index in range(len(idxs)):
        starting_indices = idxs[minibatch_index, mpi_rank]
        x = np.zeros((n_batch, n_timesteps + 1), dtype=np.uint8)
        for i, start_idx in enumerate(starting_indices):
            x[i, :] = X[start_idx:start_idx + n_timesteps + 1]
        yield x[:, :-1], x[:, 1:]


def print_rank0(*args):
    if mpi_rank == 0:
        print(*args)


if __name__ == '__main__':

    np.random.seed(0)
    parser = argparse.ArgumentParser()

    parser.add_argument('--n_epochs',          type=int,   default=100)
    parser.add_argument('--n_batch',           type=int,   default=32)
    parser.add_argument('--n_state',           type=int,   default=512)
    parser.add_argument('--n_head',            type=int,   default=4)
    parser.add_argument('--n_layer',           type=int,   default=6)
    parser.add_argument('--n_timesteps',       type=int,   default=320)
    parser.add_argument('--n_vocab',           type=int,   default=256)
    parser.add_argument('--lr',                type=float, default=0.0005)
    parser.add_argument('--cost_scale',        type=float, default=2.0**16)
    parser.add_argument('--cost_count',        type=int,   default=2000)
    parser.add_argument('--clip_norm',         type=float, default=1.0)
    parser.add_argument('--warmup_iters',      type=int,   default=1000)
    parser.add_argument('--enwik8_path',       type=str,   default='/home/scott/datasets/enwik8')
    parser.add_argument('--log_every_n_iters', type=int,   default=200)
    parser.add_argument('--profile',           type=int,   default=0)
    parser.add_argument('--log_stats',         type=int,   default=0)

    hps = parser.parse_args()

    comm = MPI.COMM_WORLD
    mpi_size = comm.Get_size()
    mpi_rank = comm.Get_rank()

    n_train = int(90e6)
    n_valid = int(5e6)
    n_test  = int(5e6)
    trainX, validX, testX = enwik8(hps.enwik8_path, n_train, n_valid, n_test)

    with tf.device("/gpu:0"):
        X = tf.placeholder(tf.uint8, shape=[hps.n_batch, hps.n_timesteps])
        Y = tf.placeholder(tf.uint8, shape=[hps.n_batch, hps.n_timesteps])

    # cost_scale and grad_scale are host side scalars
    with tf.device("/cpu:0"):
        cost_scale = tf.placeholder(tf.float32, shape=[])
        grad_scale = tf.constant(1.0) / (cost_scale * tf.constant(float(mpi_size)))

    # initialize the cost_scale placeholder value
    cur_cost_scale = hps.cost_scale
    cost_count = 0

    loss, train_op, gn, ns = model(X, Y, cost_scale, grad_scale)

    config = tf.ConfigProto()
    config.gpu_options.visible_device_list = str(mpi_rank)
    config.allow_soft_placement = True

    iteration = 0
    with tf.Session(config=config) as sess:

        sess.run(tf.global_variables_initializer())
        if mpi_size > 1:
            sess.run(sync_variables_op(mpi_rank))

        for i in range(hps.n_epochs):
            print_rank0(f'Starting epoch {i}')
            for x, y in iter_data(trainX, hps.n_timesteps, hps.n_batch, mpi_rank, mpi_size):

                cost, global_norm, norm_scale, _ = sess.run([loss, gn, ns, train_op], {X: x, Y: y, cost_scale: cur_cost_scale})

                # slowly increase cost scale but quickly drop it when inf or nan is detected in the gradients
                # norm_scale will be zero when this happens
                if norm_scale == 0.0:
                    cur_cost_scale *= 0.5
                    cost_count      = 0
                    print_rank0("fp16 saturation detected (%f), changing cost_scale to: 2^%.0f" % (global_norm, np.log2(cur_cost_scale)))
                elif cost_count >= hps.cost_count:
                    cur_cost_scale *= 2.0
                    cost_count      = 0
                    print_rank0("No fp16 saturation detected after %d iterations, changing cost_scale to: 2^%.0f" % (hps.cost_count, np.log2(cur_cost_scale)))
                else:
                    cost_count += 1

                if iteration % hps.log_every_n_iters == 0:
                    print_rank0('train iteration: %7d, loss: %.5f, bits per byte: %.5f ns:%.5f gn:%.5f' % (iteration, cost, cost/np.log(2), norm_scale, global_norm))
                iteration += 1

                if hps.profile and iteration >= hps.profile:
                    exit()


            print_rank0('Calculating validation loss')
            valid_losses = []
            for x, y in iter_data(validX, hps.n_timesteps, hps.n_batch, mpi_rank, mpi_size):

                valid_losses.append(sess.run(loss, {X: x, Y: y, cost_scale: cur_cost_scale}))

            avg_valid = sum(valid_losses) / len(valid_losses)
            print_rank0('Average validation loss: %.5f, bits per byte: %.5f' % (avg_valid, avg_valid/np.log(2)))


        print_rank0('Calculating test loss')
        test_losses = []
        for x, y in iter_data(testX, hps.n_timesteps, hps.n_batch, mpi_rank, mpi_size):

            test_losses.append(sess.run(loss, {X: x, Y: y, cost_scale: cur_cost_scale}))

        avg_test = sum(test_losses) / len(test_losses)
        print_rank0('Average test loss: %.5f, bits per byte: %.5f' % (avg_test, avg_test/np.log(2)))
