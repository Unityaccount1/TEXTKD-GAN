import tensorflow.compat.v1 as tf
from utils import Corpus, batchify, get_string
from utils_debug import sentence_to_batch, get_string
import time
import numpy as np
from layers import MLP_D, MLP_G, Seq2SeqLayer, LeakyReluActivation, LinearLayer, NormalInitializer, RandomUniformInitializer, cost
import random
import argparse

parser = argparse.ArgumentParser(description='Tensorflow ARAE for Text')

# Path Arguments
parser.add_argument('--data_path', type=str, required=True, help='location of the data corpus')
parser.add_argument('--outf', type=str, default='example', help='output directory name')

# Data Processing Arguments
parser.add_argument('--vocab_size', type=int, default=11000, help='cut vocabulary down to this size (most frequently seen words in train)')
parser.add_argument('--maxlen', type=int, default=30, help='maximum sentence length')
parser.add_argument('--lowercase', action='store_true', help='lowercase all text')

# Model Arguments
parser.add_argument('--emsize', type=int, default=300, help='size of word embeddings')
parser.add_argument('--nhidden', type=int, default=300, help='number of hidden units per layer')
parser.add_argument('--nlayers', type=int, default=1, help='number of layers')
parser.add_argument('--noise_radius', type=float, default=0.2, help='stdev of noise for autoencoder (regularizer)')
parser.add_argument('--noise_anneal', type=float, default=0.995, help='anneal noise_radius exponentially by this every 100 iterations')
parser.add_argument('--hidden_init', action='store_true', help="initialize decoder hidden state with encoder's")
parser.add_argument('--arch_g', type=str, default='300-300', help='generator architecture (MLP)')
parser.add_argument('--arch_d', type=str, default='300-300', help='critic/discriminator architecture (MLP)')
parser.add_argument('--z_size', type=int, default=100, help='dimension of random noise z to feed into generator')
parser.add_argument('--temp', type=float, default=1, help='softmax temperature (lower --> more discrete)')
parser.add_argument('--enc_grad_norm', type=bool, default=True, help='norm code gradient from critic->encoder')
parser.add_argument('--gan_toenc', type=float, default=-0.01, help='weight factor passing gradient from gan to encoder')
parser.add_argument('--dropout', type=float, default=0.0, help='dropout applied to layers (0 = no dropout)')

# Training Arguments
parser.add_argument('--epochs', type=int, default=15, help='maximum number of epochs')
parser.add_argument('--min_epochs', type=int, default=6, help="minimum number of epochs to train for")
parser.add_argument('--patience', type=int, default=5, help="number of language model evaluations without ppl improvement to wait before early stopping")
parser.add_argument('--batch_size', type=int, default=64, metavar='N', help='batch size')
parser.add_argument('--niters_ae', type=int, default=1, help='number of autoencoder iterations in training')
parser.add_argument('--niters_gan_d', type=int, default=5, help='number of discriminator iterations in training')
parser.add_argument('--niters_gan_g', type=int, default=1, help='number of generator iterations in training')
parser.add_argument('--niters_gan_schedule', type=str, default='2-4-6', help='epoch counts to increase number of GAN training iterations (increment by 1 each time)')
parser.add_argument('--lr_ae', type=float, default=1, help='autoencoder learning rate')
parser.add_argument('--lr_gan_g', type=float, default=5e-05, help='generator learning rate')
parser.add_argument('--lr_gan_d', type=float, default=1e-05, help='critic/discriminator learning rate')
parser.add_argument('--beta1', type=float, default=0.9, help='beta1 for adam. default=0.9')
parser.add_argument('--clip', type=float, default=1, help='gradient clipping, max norm')
parser.add_argument('--gan_clamp', type=float, default=0.01, help='WGAN clamp')

# Evaluation Arguments
parser.add_argument('--sample', action='store_true', help='sample when decoding for generation')
parser.add_argument('--N', type=int, default=5, help='N-gram order for training n-gram language model')
parser.add_argument('--log_interval', type=int, default=200, help='interval to log autoencoder training results')

# Other
parser.add_argument('--seed', type=int, default=1111, help='random seed')

args = parser.parse_args()

scope_autoencoder = 'autoencoder'
scope_critic = 'critic'
scope_generator = 'generator'

corpus = Corpus(args.data_path, maxlen=args.maxlen, vocab_size=args.vocab_size, lowercase=True)

# Prepare data
ntokens = len(corpus.dictionary.word2idx)
args.ntokens = ntokens

test_data = batchify(corpus.test, args.batch_size, args.maxlen, shuffle=False)
train_data = batchify(corpus.train, args.batch_size, args.maxlen, shuffle=False)

tf.reset_default_graph()

# Build graph
fixed_noise = tf.Variable(tf.random_normal(shape = (args.batch_size, args.z_size), mean=0.0, stddev=1.0, dtype=tf.float32))

with tf.variable_scope(scope_autoencoder):
    autoencoder = Seq2SeqLayer(batch_size = args.batch_size, emsize=args.emsize, nhidden=args.nhidden, ntokens=ntokens, nlayers=args.nlayers, noise_radius=args.noise_radius, hidden_init=args.hidden_init, dropout=args.dropout)
with tf.variable_scope(scope_critic):
    gan_disc = MLP_D(ninput=args.nhidden, noutput=1, layers=args.arch_d)
with tf.variable_scope(scope_generator):
    gan_gen = MLP_G(ninput=args.z_size, noutput=args.nhidden, layers=args.arch_g)

source = tf.placeholder(tf.int64, [None, args.maxlen], name = 'source') # batch_size x maxLen
target = tf.placeholder(tf.int64, [None, args.maxlen], name = 'target') # batch_size x maxLen
lengths = tf.placeholder(tf.int64, [None], name = 'lengths')
hidden_input = tf.placeholder(tf.float32, [None, args.nhidden], name = 'hidden_input')
is_train = tf.placeholder(tf.bool, name='is_train')

# Create sentence length mask over padding
output = autoencoder(source, lengths, noise=True) # batch_size x maxLen x nHidden

output_logits = output / args.temp # output: batch_size x maxLen x ntokens

output_predictions = tf.argmax(output_logits, 2) # output: batch_size x maxLen

# Loss/Accuracy for AE
loss = cost(output_logits, tf.one_hot(target, depth=args.ntokens, dtype=tf.float32))

mask = tf.logical_not(tf.equal(output_predictions, tf.constant(0, dtype = tf.int64)))

accuracy = tf.reduce_sum(tf.cast(tf.logical_and(tf.equal(output_predictions, target), mask), tf.float32), 1)
accuracy /= tf.cast(lengths, tf.float32)
accuracy = tf.reduce_mean(accuracy)

try:
    @tf.RegisterGradient("CustomGradOne")
    def constant_grad_one(unused_op, grad):
      return tf.ones_like(grad)

    @tf.RegisterGradient("CustomGradMinusOne")
    def constant_grad_minus_one(unused_op, grad):
      return -1.0 * tf.ones_like(grad)
except:
    print("Gradient hooks already registered")
  
# Generator
noise = tf.random_normal(shape = (args.batch_size, args.z_size), mean = 0, stddev = 1)
fake_hidden = gan_gen(noise)

with tf.get_default_graph().gradient_override_map({"Identity": "CustomGradOne"}):
    err_G = gan_disc(fake_hidden, reduce_mean = True)

# Discriminator/Critic
gan_disc_params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope_critic)
for p in gan_disc_params:
    tf.clip_by_value(p, -args.gan_clamp, args.gan_clamp)

real_hidden = autoencoder(source, lengths, noise=False, encode_only=True, reuse = True)

with tf.get_default_graph().gradient_override_map({"Identity": "CustomGradOne"}):
    err_D_real = gan_disc(real_hidden, reduce_mean = True)

with tf.get_default_graph().gradient_override_map({"Identity": "CustomGradMinusOne"}):
    err_D_fake = gan_disc(fake_hidden, reduce_mean = True)

autoencoder_params = tf.get_collection(scope_autoencoder)
for p in autoencoder_params:
    tf.clip_by_value(p, -args.clip, args.clip)

# Optimization

t_vars = tf.trainable_variables()
g_vars = [var for var in t_vars if scope_generator in var.name]
d_vars = [var for var in t_vars if scope_critic in var.name]

bn_update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
with tf.control_dependencies(bn_update_ops):

    # Optimizer AE

    optimizer = tf.train.GradientDescentOptimizer(learning_rate=args.lr_ae)
    gvs = optimizer.compute_gradients(loss)
    capped_gvs = [((grad if grad == None else tf.clip_by_value(grad, -args.clip, args.clip)), var) for grad, var in gvs]
    train_op_ae = optimizer.apply_gradients(capped_gvs)

    # Optimizer GAN

    train_op_g = tf.train.AdamOptimizer(learning_rate = args.lr_gan_g, beta1 = args.beta1, beta2 = 0.999).minimize(err_G, var_list=g_vars)
    train_op_d_real = tf.train.AdamOptimizer(learning_rate = args.lr_gan_d, beta1 = args.beta1, beta2 = 0.999).minimize(err_D_real, var_list=d_vars)
    train_op_d_fake = tf.train.AdamOptimizer(learning_rate = args.lr_gan_d, beta1 = args.beta1, beta2 = 0.999).minimize(err_D_fake, var_list=d_vars)

# Evaluate
max_indices = autoencoder.generate(fake_hidden, args.maxlen, sample=False, reuse = True)

max_indices_hidden = autoencoder.generate(hidden_input, args.maxlen, sample=False, reuse = True)

writer = tf.summary.FileWriter(logdir='/tmp/tensorboard', graph=tf.get_default_graph())
writer.flush()

# Train

saver = tf.train.Saver()

if args.niters_gan_schedule != "":
    gan_schedule = [int(x) for x in args.niters_gan_schedule.split("-")]
else:
    gan_schedule = []
niter_gan = 1

init = tf.global_variables_initializer()
with tf.Session() as sess:
    init.run()
    
    for epoch in range(1, args.epochs+1):
        
        if epoch in gan_schedule:
            niter_gan += 1
        
        total_loss_ae = 0
        niter = 0
        niter_global = 1
        
        start_time = time.time()

        # loop through all batches in training data
        while niter < len(train_data):
            for i in range(args.niters_ae):
                #saver.save(sess, '/data/tf-models/arae/arae-tf-120118-iter', global_step=i)
                
                if niter == len(train_data):
                    break  # end of epoch
                source_batch, target_batch, lengths_batch = train_data[niter]
                _, loss_val, acc_val = sess.run([train_op_ae, loss, accuracy], {source: source_batch, target: target_batch, lengths: lengths_batch, is_train: True})
                
                total_loss_ae += loss_val
                elapsed = time.time() - start_time
                
                if niter % args.log_interval == 0 and niter > 0:
                    cur_loss = total_loss_ae / args.log_interval
                    total_loss_ae = 0
                    print('| epoch {:3d} | {:5d}/{:5d} batches | ms/batch {:5.2f} | loss {:5.2f} | acc {:8.2f}'.format(epoch, niter, len(train_data), elapsed * 1000 / niter, cur_loss, acc_val))

                niter += 1  

            for k in range(niter_gan):
                for i in range(args.niters_gan_d):
                    source_batch, target_batch, lengths_batch = train_data[random.randint(0, len(train_data)-1)]
                    _, _, _, err_D_fake_val, err_D_real_val = sess.run([train_op_d_real, train_op_d_fake, train_op_ae, err_D_fake, err_D_real], {source: source_batch, target: target_batch, lengths: lengths_batch, is_train: True})
                    
                for i in range(args.niters_gan_g):
                    source_batch, target_batch, lengths_batch = train_data[random.randint(0, len(train_data)-1)]
                    _, G_loss_val = sess.run([train_op_g, err_G], {source: source_batch, target: target_batch, lengths: lengths_batch, is_train: True})
                    
            niter_global += 1
            
            if niter_global % 100 == 0:
                autoencoder.noise_radius = autoencoder.noise_radius*args.noise_anneal
                
                print('[%d/%d][%d/%d] Loss_D: %.8f (Loss_D_real: %.8f Loss_D_fake: %.8f) Loss_G: %.8f' % (epoch, args.epochs, niter, len(train_data), err_D_fake_val - err_D_real_val, err_D_real_val, err_D_fake_val, G_loss_val))
                
                if niter_global % 300 == 0:
                    source_batch, target_batch, lengths_batch = train_data[random.randint(0, len(train_data)-1)]
                    max_ind = sess.run([max_indices], {source: source_batch, target: target_batch, lengths: lengths_batch, is_train: True})
                    print('Evaluating generator: %s' % get_string(max_ind[0], corpus))

