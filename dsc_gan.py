import tensorflow as tf
import numpy as np
from tensorflow.contrib import layers
import scipy.io as sio
from scipy.sparse.linalg import svds
from skcuda.linalg import svd as svd_cuda
import pycuda.gpuarray as gpuarray
from pycuda.tools import DeviceMemoryPool
from sklearn import cluster
from sklearn.preprocessing import normalize
from munkres import Munkres
import os
import time
import argparse

mem_pool = DeviceMemoryPool()

parser = argparse.ArgumentParser()
parser.add_argument('name')
parser.add_argument('--lambda3',    type=float, default=1.0)


class ConvAE(object):
    def __init__(self,
            n_input, n_hidden, kernel_size, n_class,
            lambda1, lambda2, lambda3, batch_size,
            reg=None, disc_bound=0.02,
            model_path = None, restore_path = None,
            logs_path = 'logs'):
        self.n_class = n_class
        self.n_input = n_input
        self.kernel_size = kernel_size
        self.n_hidden = n_hidden
        self.batch_size = batch_size
        self.reg = reg
        self.model_path = model_path
        self.restore_path = restore_path
        self.iter = 0
        latent_size = 1080

        #input required to be fed
        self.x = tf.placeholder(tf.float32, [None, n_input[0], n_input[1], 1])
        self.learning_rate = tf.placeholder(tf.float32, [])

        # weights
        weights = self.make_weights()
        disc_weights  = [v for v in tf.trainable_variables() if v.name.startswith('disc')]
        other_weights = [v for v in tf.trainable_variables() if v not in disc_weights]

        # run input through encoder
        latent, shape = self.encoder(self.x, weights)

        # self-expressive layer
        z = tf.reshape(latent, [batch_size, -1])
        Coef = weights['Coef']
        z_c = tf.matmul(Coef,z)
        self.Coef = Coef
        latent_c = tf.reshape(z_c, tf.shape(latent)) # petential problem here
        self.z = z

        # run self-expressive's output through decoder
        self.x_r = self.decoder(latent_c, weights, shape)

        # Eqn 3 loss
        self.loss_recon = 0.5 * tf.reduce_sum(tf.pow(tf.subtract(self.x_r, self.x), 2.0))
        self.loss_sparsity = tf.reduce_sum(tf.pow(self.Coef,2.0))
        self.loss_selfexpress = 0.5 * tf.reduce_sum(tf.pow(tf.subtract(z_c, z), 2.0))
        self.loss_eqn3 = self.loss_recon + lambda1 * self.loss_sparsity + lambda2 * self.loss_selfexpress
        self.optimizer_eqn3 = tf.train.AdamOptimizer(learning_rate=self.learning_rate).minimize(self.loss_eqn3, var_list=other_weights)

        # discriminator loss
        self.y_x    = tf.placeholder(tf.int32, [None])
        self.z_real = z
        self.z_fake = self.make_z_fake(self.z_real, self.y_x, self.n_class, 64)
        self.score_disc = self.score_discriminator(self.z_real, self.z_fake, weights)
        self.optimizer_disc = tf.train.AdamOptimizer(learning_rate=self.learning_rate).minimize(-self.score_disc, var_list=disc_weights)
        self.clip_weight = [w.assign(tf.clip_by_value(w, -disc_bound, disc_bound)) for w in disc_weights]

        # Eqn 3 + generator loss
        self.loss_eqn3plus = self.loss_eqn3 + lambda3 * self.score_disc
        self.optimizer_eqn3plus = tf.train.AdamOptimizer(learning_rate=self.learning_rate).minimize(self.loss_eqn3plus, var_list=other_weights)

        # finalize stuffs
        s1 = tf.summary.scalar("loss_recon",       self.loss_recon)
        s2 = tf.summary.scalar("loss_sparsity",    self.loss_sparsity)
        s3 = tf.summary.scalar("loss_selfexpress", self.loss_selfexpress)
        s4 = tf.summary.scalar("score_disc",       self.score_disc)
        self.summaryop_eqn3     = tf.summary.merge([s1, s2, s3])
        self.summaryop_eqn3plus = tf.summary.merge([s1, s2, s3, s4])
        self.init = tf.global_variables_initializer()
        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True  # stop TF from eating up all GPU RAM 
        config.gpu_options.per_process_gpu_memory_fraction = 0.4
        self.sess = tf.InteractiveSession(config=config)
        self.sess.run(self.init)
        self.saver = tf.train.Saver([v for v in tf.trainable_variables() if not (v.name.startswith("Coef") or v.name.startswith('disc'))])
        self.summary_writer = tf.summary.FileWriter(logs_path, graph=tf.get_default_graph())

    def make_weights(self):
        all_weights = dict()
        with tf.device('/gpu:0'):
            # AE weights + C weights
            all_weights['enc_w0'] = tf.get_variable("enc_w0", shape=[self.kernel_size[0], self.kernel_size[0], 1, self.n_hidden[0]],
                initializer=layers.xavier_initializer_conv2d(),regularizer = self.reg)
            all_weights['enc_b0'] = tf.Variable(tf.zeros([self.n_hidden[0]], dtype = tf.float32))

            all_weights['enc_w1'] = tf.get_variable("enc_w1", shape=[self.kernel_size[1], self.kernel_size[1], self.n_hidden[0],self.n_hidden[1]],
                initializer=layers.xavier_initializer_conv2d(),regularizer = self.reg)
            all_weights['enc_b1'] = tf.Variable(tf.zeros([self.n_hidden[1]], dtype = tf.float32))

            all_weights['enc_w2'] = tf.get_variable("enc_w2", shape=[self.kernel_size[2], self.kernel_size[2], self.n_hidden[1],self.n_hidden[2]],
                initializer=layers.xavier_initializer_conv2d(),regularizer = self.reg)
            all_weights['enc_b2'] = tf.Variable(tf.zeros([self.n_hidden[2]], dtype = tf.float32))

            all_weights['Coef']   = tf.Variable(1.0e-4 * tf.ones([self.batch_size, self.batch_size],tf.float32), name = 'Coef')

            all_weights['dec_w0'] = tf.get_variable("dec_w0", shape=[self.kernel_size[2], self.kernel_size[2], self.n_hidden[1],self.n_hidden[2]],
                initializer=layers.xavier_initializer_conv2d(),regularizer = self.reg)
            all_weights['dec_b0'] = tf.Variable(tf.zeros([self.n_hidden[1]], dtype = tf.float32))

            all_weights['dec_w1'] = tf.get_variable("dec_w1", shape=[self.kernel_size[1], self.kernel_size[1], self.n_hidden[0],self.n_hidden[1]],
                initializer=layers.xavier_initializer_conv2d(),regularizer = self.reg)
            all_weights['dec_b1'] = tf.Variable(tf.zeros([self.n_hidden[0]], dtype = tf.float32))

            all_weights['dec_w2'] = tf.get_variable("dec_w2", shape=[self.kernel_size[0], self.kernel_size[0],1, self.n_hidden[0]],
                initializer=layers.xavier_initializer_conv2d(),regularizer = self.reg)
            all_weights['dec_b2'] = tf.Variable(tf.zeros([1], dtype = tf.float32))

            # discriminator weights
            all_weights['disc_w0'] = tf.get_variable('disc_w0', shape=[1080, 200], initializer=layers.xavier_initializer())
            all_weights['disc_b0'] = tf.get_variable('disc_b0', shape=[200],       initializer=tf.zeros_initializer())
            all_weights['disc_w1'] = tf.get_variable('disc_w1', shape=[200 , 50 ], initializer=layers.xavier_initializer())
            all_weights['disc_b1'] = tf.get_variable('disc_b1', shape=[50 ],       initializer=tf.zeros_initializer())
            all_weights['disc_w2'] = tf.get_variable('disc_w2', shape=[50  , 1  ], initializer=layers.xavier_initializer())
            # final layer has no bias

        return all_weights

    # Building the encoder
    def encoder(self, x, weights):
        shapes = []
        # Encoder Hidden layer with sigmoid activation #1
        shapes.append(x.get_shape().as_list())
        layer1 = tf.nn.bias_add(tf.nn.conv2d(x, weights['enc_w0'], strides=[1,2,2,1],padding='SAME'),weights['enc_b0'])
        layer1 = tf.nn.relu(layer1)
        shapes.append(layer1.get_shape().as_list())
        layer2 = tf.nn.bias_add(tf.nn.conv2d(layer1, weights['enc_w1'], strides=[1,2,2,1],padding='SAME'),weights['enc_b1'])
        layer2 = tf.nn.relu(layer2)
        shapes.append(layer2.get_shape().as_list())
        layer3 = tf.nn.bias_add(tf.nn.conv2d(layer2, weights['enc_w2'], strides=[1,2,2,1],padding='SAME'),weights['enc_b2'])
        layer3 = tf.nn.relu(layer3)
        return  layer3, shapes

    # Building the decoder
    def decoder(self, z, weights, shapes):
        # Encoder Hidden layer with sigmoid activation #1
        shape_de1 = shapes[2]
        layer1 = tf.add(tf.nn.conv2d_transpose(z, weights['dec_w0'], tf.stack([tf.shape(self.x)[0],shape_de1[1],shape_de1[2],shape_de1[3]]),\
         strides=[1,2,2,1],padding='SAME'),weights['dec_b0'])
        layer1 = tf.nn.relu(layer1)
        shape_de2 = shapes[1]
        layer2 = tf.add(tf.nn.conv2d_transpose(layer1, weights['dec_w1'], tf.stack([tf.shape(self.x)[0],shape_de2[1],shape_de2[2],shape_de2[3]]),\
         strides=[1,2,2,1],padding='SAME'),weights['dec_b1'])
        layer2 = tf.nn.relu(layer2)
        shape_de3= shapes[0]
        layer3 = tf.add(tf.nn.conv2d_transpose(layer2, weights['dec_w2'], tf.stack([tf.shape(self.x)[0],shape_de3[1],shape_de3[2],shape_de3[3]]),\
         strides=[1,2,2,1],padding='SAME'),weights['dec_b2'])
        layer3 = tf.nn.relu(layer3)
        return layer3

    def discriminator(self, Z_input, weights):
        D0 = tf.nn.relu(tf.add(tf.matmul(Z_input, weights['disc_w0']), weights['disc_b0']))
        D1 = tf.nn.relu(tf.add(tf.matmul(D0     , weights['disc_w1']), weights['disc_b1']))
        D2 =            tf.add(tf.matmul(D1     , weights['disc_w2']), 0)
        return D2

    def score_discriminator(self, z_real, z_fake, weights):
        score_real = self.discriminator(z_real, weights)
        score_fake = self.discriminator(z_fake, weights)
        score = tf.reduce_mean(score_real - score_fake) # maximize score_real, minimize score_fake
        # a good discriminator would have a very positive score
        return score

    def make_z_fake(self, z_real, y_x, K, M):
        """
        z_real: a 2432x1080 tensor, each row is a data point
        y_x   : a 2432 vector, indicating cluster membership of each data point
        K: number of clusters
        M: number of fake samples per cluster
        """
        group_index = [tf.where(tf.equal(y_x, k))        for k in xrange(K)] # indices of datapoints in k-th cluster
        groups      = [tf.gather(z_real, group_index[k]) for k in xrange(K)] # datapoints in k-th cluster
        # for each group, take M random combination as fake samples
        combined = []
        for g in groups:
            g = tf.squeeze(g)
            N_g = tf.shape(g)[0]                                    # number of datapoints in this cluster
            selector = tf.random_uniform([M, N_g])                  # make random selector matrix
            selector = selector / tf.reduce_sum(selector, 1, keep_dims=True)# normalize each row to 1
            #print selector.shape, g.shape, z_real.shape
            selected = tf.matmul(selector, g)       # make M linear combinations
            combined.append(selected)
        z_fake = tf.concat(combined, 0) # a matrix of KxM,
        return z_fake

    def partial_fit_eqn3(self, X, lr):
        # take a step on Eqn 3/4
        cost, Coef, summary, _ = self.sess.run((self.loss_recon, self.Coef, self.summaryop_eqn3, self.optimizer_eqn3),
                feed_dict = {self.x: X, self.learning_rate: lr})
        self.summary_writer.add_summary(summary, self.iter)
        self.iter += 1
        return cost, Coef

    def partial_fit_disc(self, X, y_x, lr):
        assert y_x.min() == 0, 'y_x is 0-based'
        self.sess.run([self.optimizer_disc, self.clip_weight], feed_dict={self.x:X, self.y_x:y_x, self.learning_rate:lr})

    def partial_fit_eqn3plus(self, X, y_x, lr):
        assert y_x.min() == 0, 'y_x is 0-based'
        cost, Coef, summary, _ = self.sess.run([self.loss_recon, self.Coef, self.summaryop_eqn3plus, self.optimizer_eqn3plus], 
                feed_dict={self.x:X, self.y_x:y_x, self.learning_rate:lr})
        self.summary_writer.add_summary(summary, self.iter)
        self.iter += 1
        return cost, Coef

    def initlization(self):
        self.sess.run(self.init)

    def reconstruct(self,X):
        return self.sess.run(self.x_r, feed_dict = {self.x:X})

    def transform(self, X):
        return self.sess.run(self.z, feed_dict = {self.x:X})

    def save_model(self):
        save_path = self.saver.save(self.sess,self.model_path)
        print ("model saved in file: %s" % save_path)

    def restore(self):
        self.saver.restore(self.sess, self.restore_path)
        print ("model restored")

    def check_size(self, X):
        z = self.sess.run(self.z, feed_dict={self.x:X})
        return z


def best_map(L1,L2):
    #L1 should be the groundtruth labels and L2 should be the clustering labels we got
    Label1 = np.unique(L1)
    nClass1 = len(Label1)
    Label2 = np.unique(L2)
    nClass2 = len(Label2)
    nClass = np.maximum(nClass1,nClass2)
    G = np.zeros((nClass,nClass))
    for i in range(nClass1):
        ind_cla1 = L1 == Label1[i]
        ind_cla1 = ind_cla1.astype(float)
        for j in range(nClass2):
            ind_cla2 = L2 == Label2[j]
            ind_cla2 = ind_cla2.astype(float)
            G[i,j] = np.sum(ind_cla2 * ind_cla1)
    m = Munkres()
    index = m.compute(-G.T)
    index = np.array(index)
    c = index[:,1]
    newL2 = np.zeros(L2.shape)
    for i in range(nClass2):
        newL2[L2 == Label2[i]] = Label1[c[i]]
    return newL2


def thrC(C,ro):
    if ro < 1:
        N = C.shape[1]
        Cp = np.zeros((N,N))
        S = np.abs(np.sort(-np.abs(C),axis=0))
        Ind = np.argsort(-np.abs(C),axis=0)
        for i in range(N):
            cL1 = np.sum(S[:,i]).astype(float)
            stop = False
            csum = 0
            t = 0
            while(stop == False):
                csum = csum + S[t,i]
                if csum > ro*cL1:
                    stop = True
                    Cp[Ind[0:t+1,i],i] = C[Ind[0:t+1,i],i]
                t = t + 1
    else:
        Cp = C

    return Cp


def build_aff(C):
    N = C.shape[0]
    Cabs = np.abs(C)
    ind = np.argsort(-Cabs,0)
    for i in range(N):
        Cabs[:,i]= Cabs[:,i] / (Cabs[ind[0,i],i] + 1e-6)
    Cksym = Cabs + Cabs.T;
    return Cksym


def spectral_cluster(L, n, eps=2.2*10-8):
    """
    L: Laplacian
    n: number of clusters
    Translates MATLAB code below:
    N  = size(L, 1)
    DN = diag( 1./sqrt(sum(L)+eps) );
    LapN = speye(N) - DN * L * DN;
    [~,~,vN] = svd(LapN);
    kerN = vN(:,N-n+1:N);
    normN = sum(kerN .^2, 2) .^.5;
    kerNS = bsxfun(@rdivide, kerN, normN + eps);
    groups = kmeans(kerNS,n,'maxiter',MAXiter,'replicates',REPlic,'EmptyAction','singleton');
    """
    N  = L.shape[0]
    DN = (1. / np.sqrt(L.sum(0)+eps))
    LapN = np.eye(N) - DN * L * DN


def post_proC(C, K, d, alpha):
    # C: coefficient matrix, K: number of clusters, d: dimension of each subspace
    C = 0.5*(C + C.T)
    r = d*K + 1 # K=38, d=10
    t_begin = time.time()
    # U, S, _ = svds(C,r,v0 = np.ones(C.shape[0]))
    U, S, _ = svd_cuda(C, allocator=mem_pool)
    # take U and S from GPU
    # U = U[:, :r].get()
    # S = S[:r].get()
    t_end = time.time()
    print 'time1 = {}'.format(t_end - t_begin)
    U = U[:,::-1]
    S = np.sqrt(S[::-1])
    S = np.diag(S)
    U = U.dot(S)
    U = normalize(U, norm='l2', axis = 1)
    Z = U.dot(U.T)
    Z = Z * (Z>0)
    L = np.abs(Z ** alpha)
    L = L/L.max()
    L = 0.5 * (L + L.T)
    t_begin = time.time()
    spectral = cluster.SpectralClustering(n_clusters=K, eigen_solver='arpack', affinity='precomputed',assign_labels='discretize')
    spectral.fit(L)
    grp = spectral.fit_predict(L) # +1
    t_end = time.time()
    print 'time2 = {}'.format(t_end - t_begin)
    return grp, L


def err_rate(gt_s, s):
    c_x = best_map(gt_s,s)
    err_x = np.sum(gt_s[:] != c_x[:])
    missrate = err_x.astype(float) / (gt_s.shape[0])
    return missrate


def build_laplacian(C):
    C = 0.5 * (np.abs(C) + np.abs(C.T))
    W = np.sum(C,axis=0)
    W = np.diag(1.0/W)
    L = W.dot(C)
    return L


def reinit_and_optimize(Img, Label, CAE, n_class, num_epochs=None):
    alpha = max(0.4 - (n_class-1)/10 * 0.1, 0.1)
    print alpha

    acc_= []
    # one loop per subject subset
    # if n_class==38, only one subset
    # if n_class==10, 29 subsets
    for i in range(0,39-n_class):
        CAE.initlization()
        CAE.restore() # restore from pre-trained model

        subset_imgs   = np.array(Img[64*i:64*(i+n_class),:])
        subset_imgs   = subset_imgs.astype(float)
        subset_labels = np.array(Label[64*i:64*(i+n_class)])
        subset_labels = subset_labels - subset_labels.min() #+1
        subset_labels = np.squeeze(subset_labels)

        num_epochs =  num_epochs or 50 + n_class*25# 100+n_class*20
        update_interval = 100 # every so many epochs, we recompute the clustering
        lr = 1.0e-3
        # fine-tune network
        print 'Optimize for {} steps'.format(num_epochs)
        clustered = False
        assert num_epochs == 1000, 'Currently hard-coded for 1000 epochs'
        num_epochs += 2000 # add 2000 GAN epochs
        for epoch in xrange(1, num_epochs+1):
            if epoch % 10 == 0:
                print 'epoch {}'.format(epoch)
            """
            First 1000 epochs, just train on eqn3
            Subsequent epochs, train on eqn3plus
            """
            if epoch <= 1000:
                cost, Coef = CAE.partial_fit_eqn3(subset_imgs, lr)
            else:
                CAE.partial_fit_disc(subset_imgs, y_x, lr)  # discriminator step discriminator
                cost, Coef = CAE.partial_fit_eqn3plus(subset_imgs, y_x, lr)
            # every 100 epochs, 
            if epoch % update_interval == 0:
                print "epoch: %.1d" % epoch, "cost: %.8f" % (cost/float(batch_size))
                Coef = thrC(Coef,alpha)
                t_begin = time.time()
                y_x, _ = post_proC(Coef, n_class, 10, 3.5)
                missrate_x = err_rate(subset_labels, y_x)
                t_end = time.time()
                acc_x = 1 - missrate_x
                print "experiment: %d" % i, "our accuracy: %.4f" % acc_x
                print 'post processing time: {}'.format(t_end - t_begin)
                clustered = True
        acc_.append(acc_x)

    acc_ = np.array(acc_)
    m = np.mean(acc_)
    me = np.median(acc_)
    print("%d subjects:" % n_class)
    print("Mean: %.4f%%" % ((1-m)*100))
    print("Median: %.4f%%" % ((1-me)*100))
    print(acc_)

    return (1-m), (1-me)


if __name__ == '__main__':
    args = parser.parse_args()
    assert args.name is not None and args.name != '', 'name of experiment must be specified'

    folder = os.path.dirname(os.path.abspath(__file__))
    # load face images and labels
    data = sio.loadmat(os.path.join(folder, 'YaleBCrop025.mat'))
    img = data['Y']

    # Reorganize data a bit, put images into Img, and labels into Label
    I = []
    Label = []
    for i in range(img.shape[2]):       # i-th subject
        for j in range(img.shape[1]):   # j-th picture of i-th subject
            temp = np.reshape(img[:,j,i],[42,48])
            Label.append(i)
            I.append(temp)
    I = np.array(I)
    Label = np.array(Label[:])
    Img = np.transpose(I,[0,2,1])
    Img = np.expand_dims(Img[:],3)

    # configuration for conv-AE
    n_input = [48,42]
    kernel_size = [5,3,3]
    n_hidden = [10,20,30]

    all_subjects = [38] # [10, 15, 20, 25, 30, 35, 38]

    avg = []
    med = []

    # for each experiment setting, perform one loop
    for n_class in all_subjects:
        batch_size = n_class * 64

        lambda1 = 1.0                                   # L2 sparsity on C
        lambda2 = 1.0 * 10 ** (n_class / 10.0 - 3.0)    # self-expressivity
        lambda3 = args.lambda3                          # discriminator gradient

        model_path   = os.path.join(folder, 'model-102030-48x42-yaleb.ckpt')
        logs_path    = os.path.join(folder, 'logs', args.name)
        restore_path = model_path

        # clear graph and build a new conv-AE
        tf.reset_default_graph()
        CAE = ConvAE(
                n_input, n_hidden, kernel_size, n_class,
                lambda1, lambda2, lambda3, batch_size,
                model_path=model_path, restore_path=restore_path, logs_path=logs_path)

        # perform optimization
        avg_i, med_i = reinit_and_optimize(Img, Label, CAE, n_class)
        # add result to list
        avg.append(avg_i)
        med.append(med_i)

    # report results for all experiments
    for i, n_class in enumerate(all_subjects):
        print '%d subjects:' % n_class
        print 'Mean: %.4f%%' % (avg[i]*100), 'Median: %.4f%%' % (med[i]*100)

