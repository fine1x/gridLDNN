import tensorflow as tf
import os
from glob import glob
import sys
import logging

import time
from basic.batch_generation import get_filepaths
from get_train_pathlength import get_indexpath
from hyperband.common_defs import *
'''
For block_intepreter with rectangle 
'''
logger = logging.getLogger(__name__)
os.environ["CUDA_VISIBLE_DEVICES"] = "3"


class HyperParameters:
    def __init__(self):
        self.LEARNING_RATE = 0.001
        self.NUM_HIDDEN = 1024
        self.OUTPUT_THRESHOLD = 0.5
        self.BATCH_SIZE = 60
        self.EPOCHS = 30

        self.NUM_CLASSES = 13

        self.TRAIN_SET = self.get_train_rectangle()
        self.TEST_SET = self.get_test_rectangle()

        self.TOTAL_SAMPLES = len(self.PATHS)
        self.NUM_TRAIN = len(self.TRAIN_SET)
        self.NUM_TEST = len(self.TEST_SET)
        self.SET = {'train': self.TRAIN_SET,
               'test': self.TEST_SET}

    def get_train_rectangle(self):
        tt = time.time()
        self.PATHS = []
        for f in range(1, 7):
            p = '/mnt/raid/data/ni/twoears/scenes2018/train/fold' + str(f) + '/scene1'
            path = glob(p + '/**/**/*.npz', recursive=True)
            self.PATHS += path
        INDEX_PATH = get_indexpath(self.PATHS)
        out = get_filepaths(self.EPOCHS, 4000, INDEX_PATH)
        print("Construt rectangel time:",time.time()-tt)
        return out
    def get_test_rectangle(self):
        self.DIR_TEST = '/mnt/raid/data/ni/twoears/scenes2018/train/fold1/scene10'
        PATH_TEST = glob(self.DIR_TEST + '/*.npz', recursive=True)
        INDEX_PATH_TEST = get_indexpath(PATH_TEST)
        return get_filepaths(1, 4000, INDEX_PATH_TEST)

    def _read_py_function(self,filename):
        filename = filename.decode(sys.getdefaultencoding())
        fx, fy = np.array([]).reshape(0, 160), np.array([]).reshape(0, 13)
        # each filename is : path1&start_index&end_index@path2&start_index&end_index
        # the total length was defined before
        for instance in filename.split('@'):
            p, start, end = instance.split('&')
            data = np.load(p)
            x = np.reshape(data['x'], [-1, 160])
            y = np.transpose(data['y'])
            y[y == 0] = 2
            y[np.isnan(y)] = 3
            fx = np.concatenate((fx, x[int(start):int(end)]), axis=0)
            fy = np.concatenate((fy, y[int(start):int(end)]), axis=0)
        l = np.array([fx.shape[0]])
        return fx.astype(np.float32), fy.astype(np.int32), l.astype(np.int32)

    def read_dataset(self,path_set, batchsize):
        dataset = tf.data.Dataset.from_tensor_slices(path_set)
        dataset = dataset.map(
            lambda filename: tuple(tf.py_func(self._read_py_function, [filename], [tf.float32, tf.int32, tf.int32])))
        batch = dataset.padded_batch(batchsize, padded_shapes=([None, None], [None, None], [None]))
        return batch

    def RNN(self,x, weights, seq):

        # Forward direction cell
        # orthogonal_initializer
        with tf.variable_scope('lstm', initializer=tf.orthogonal_initializer()):
            lstm_ell = tf.contrib.rnn.BasicLSTMCell(self.NUM_HIDDEN, forget_bias=1)
            # stack = tf.contrib.rnn.MultiRNNCell([cell] * 2, state_is_tuple=True)
            batch_x_shape = tf.shape(x)
            layer = tf.reshape(x, [batch_x_shape[0], -1, 160])
            # defining initial state
            # initial_state = rnn_cell.zero_state(batch_size, dtype=tf.float32)
            outputs, output_states = tf.nn.dynamic_rnn(cell=lstm_ell,
                                                       inputs=layer,
                                                       dtype=tf.float32,
                                                       time_major=False,
                                                       sequence_length=seq
                                                       )

            outputs = tf.reshape(outputs, [-1, self.NUM_HIDDEN])
            top = tf.matmul(outputs, weights['out'])
            original_out = tf.reshape(top, [batch_x_shape[0], -1, self.NUM_CLASSES])
        return original_out




    def main(self):
        # tensor holder
        train_batch = self.read_dataset(self.SET['train'], self.BATCH_SIZE)
        test_batch = self.read_dataset(self.SET['test'], self.BATCH_SIZE)

        handle = tf.placeholder(tf.string, shape=[])
        iterator = tf.data.Iterator.from_string_handle(handle, train_batch.output_types, train_batch.output_shapes)
        X, Y, seq = iterator.get_next()
        # get mask matrix for loss fuction, will be used after round output
        mask_padding = tf.cast(tf.not_equal(Y, 0), tf.int32)
        mask_negative = tf.cast(tf.not_equal(Y, 2), tf.int32)
        mask_zero_frames = tf.cast(tf.not_equal(Y, -1), tf.int32)
        mask_nan = tf.cast(tf.not_equal(Y, 3), tf.int32)
        seq = tf.reshape(seq, [self.BATCH_SIZE])  # original sequence length, only used for RNN

        train_iterator = train_batch.make_initializable_iterator()
        test_iterator = test_batch.make_initializable_iterator()
        # Define weights
        weights = {
            # Hidden layer weights => 2*n_hidden because of forward + backward cells
            'out': tf.Variable(tf.random_normal([self.NUM_HIDDEN, self.NUM_CLASSES]))
        }

        # logits = [batch_size,time_steps,number_class]
        logits = self.RNN(X, weights, seq)

        # Define loss and optimizer
        positive_weight = [0.093718168209890373, 0.063907567921264216, 0.067798105106531739, 0.18291906814983463,
                           0.060061489920493351, 0.0300554843451682, 0.14020777497915976, 0.098981561987397257,
                           0.02414707385064941, 0.032517232415765082, 0.07860240402283912, 0.073578874716527881,
                           0.053505194374478995]

        negative_weight = [0.90628183179010957, 0.93609243207873583, 0.93220189489346827, 0.81708093185016539,
                           0.93993851007950668, 0.96994451565483175, 0.8597922250208403, 0.90101843801260273,
                           0.9758529261493506, 0.9674827675842349, 0.92139759597716087, 0.92642112528347209,
                           0.94649480562552102]

        w = [y / x for x, y in zip(positive_weight, negative_weight)]

        with tf.variable_scope('loss'):
            # convert 2(-1) to 0
            mask_Y = Y * mask_negative
            # convert nan to +1
            add_nan_one = tf.ones(tf.shape(mask_nan), dtype=tf.int32) - mask_nan
            mask_Y = tf.add(mask_Y * mask_nan, add_nan_one)

            # assign 0 frames zero cost
            number_zero_frame = tf.reduce_sum(tf.cast(tf.equal(Y, -1), tf.int32))
            # mask_Y = mask_Y*mask_zero_frames
            # mask_logits = logits*tf.cast(mask_zero_frames,tf.float32)
            # treat NaN as +1 in training, assign NaN frames zero cost in testing
            loss_op = tf.nn.weighted_cross_entropy_with_logits(tf.cast(mask_Y, tf.float32), logits, tf.constant(w))
            # number of frames without zero_frame
            total = tf.cast(tf.reduce_sum(seq) - number_zero_frame, tf.float32)
            # eliminate zero_frame loss
            loss_op = tf.reduce_sum(loss_op * tf.cast(mask_zero_frames, tf.float32)) / total
        with tf.variable_scope('optimize'):
            optimizer = tf.train.AdamOptimizer(learning_rate=self.LEARNING_RATE)
            train_op = optimizer.minimize(loss_op)
        with tf.name_scope("accuracy"):
            # add a threshold to round the output to 0 or 1
            # logits is already being sigmoid
            predicted = tf.to_int32(tf.sigmoid(logits) > self.OUTPUT_THRESHOLD)
            TP = tf.count_nonzero(predicted * mask_Y * mask_padding * mask_zero_frames)
            # mask padding, zero_frame,
            TN = tf.count_nonzero((predicted - 1) * (mask_Y - 1) * mask_padding * mask_zero_frames)
            FP = tf.count_nonzero(predicted * (mask_Y - 1) * mask_padding * mask_zero_frames)
            FN = tf.count_nonzero((predicted - 1) * mask_Y * mask_padding * mask_zero_frames)
            precision = TP / (TP + FP)
            recall = TP / (TP + FN)
            f1 = 2 * precision * recall / (precision + recall)
            # TPR = TP/(TP+FN)
            sensitivity = recall
            specificity = TN / (TN + FP)

        # Initialize the variables (i.e. assign their default value)
        init = tf.global_variables_initializer()
        logging.basicConfig(level=logging.DEBUG,format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
        # logging.basicConfig(level=logging.DEBUG,format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',filename='./log3-14-rectangle.txt')

        logger = logging.getLogger(os.path.basename(__file__))
        tf.logging.set_verbosity(tf.logging.INFO)

        # Start training
        with tf.Session() as sess:
            logger.info('''
                                    Epochs: {}
                                    Number of hidden neuron: {}
                                    Batch size: {}'''.format(
                self.EPOCHS,
                self.NUM_HIDDEN,
                self.BATCH_SIZE))
            train_handle = sess.run(train_iterator.string_handle())
            test_handle = sess.run(test_iterator.string_handle())
            # Run the initializer
            sess.run(init)

            section = '\n{0:=^40}\n'
            logger.info(section.format('Run training epoch'))
            # final_average_loss = 0.0


            ee = 1
            # initialization for each epoch
            final_train, final_test = 0.0, 0.0
            train_cost, sen, spe, f = 0.0, 0.0, 0.0, 0.0

            epoch_start = time.time()

            sess.run(train_iterator.initializer)
            n_batches = int(self.NUM_TRAIN / self.BATCH_SIZE)
            batch_per_epoch = int(n_batches / self.EPOCHS)
            # print(sess.run([seq, train_op],feed_dict={handle:train_handle}))
            for num in range(1, n_batches + 1):
                loss, _, se, sp, tempf1 = sess.run([loss_op, train_op, sensitivity, specificity, f1],
                                                   feed_dict={handle: train_handle})
                logger.debug(
                    'Train cost: %.2f | Accuracy: %.2f | Sensitivity: %.2f | Specificity: %.2f| F1-score: %.2f',
                    loss, (se + sp) / 2, se, sp, tempf1)
                train_cost = train_cost + loss
                sen = sen + se
                spe = spe + sp
                f = tempf1 + f
                #     final_average_loss = train_cost / n_batches
                # return final_average_loss
                if (num % batch_per_epoch == 0):
                    epoch_duration0 = time.time() - epoch_start
                    logger.info(
                        '''Epochs: {},train_cost: {:.3f},Train_accuracy: {:.3f},Sensitivity: {:.3f},Specificity: {:.3f},F1-score: {:.3f},time: {:.2f} sec'''
                            .format(ee + 1,
                                    train_cost / batch_per_epoch,
                                    ((sen + spe) / 2) / batch_per_epoch,
                                    sen / batch_per_epoch,
                                    spe / batch_per_epoch,
                                    f / batch_per_epoch,
                                    epoch_duration0))
                    final_train = ((sen + spe) / 2) / batch_per_epoch
                    # for validation
                    train_cost, sen, spe, f = 0.0, 0.0, 0.0, 0.0
                    v_batches_per_epoch = int(self.NUM_TEST / self.BATCH_SIZE)
                    epoch_start = time.time()
                    sess.run(test_iterator.initializer)
                    for _ in range(v_batches_per_epoch):
                        se, sp, tempf1 = sess.run([sensitivity, specificity, f1], feed_dict={handle: test_handle})
                        sen = sen + se
                        spe = spe + sp
                        f = tempf1 + f
                    epoch_duration1 = time.time() - epoch_start

                    logger.info(
                        '''Epochs: {},Validation_accuracy: {:.3f},Sensitivity: {:.3f},Specificity: {:.3f},F1 score: {:.3f},time: {:.2f} sec'''
                            .format(ee + 1,
                                    ((sen + spe) / 2) / v_batches_per_epoch,
                                    sen / v_batches_per_epoch,
                                    spe / v_batches_per_epoch,
                                    f / v_batches_per_epoch,
                                    epoch_duration1))
                    print(ee)
                    ee += 1
                    final_test = ((sen + spe) / 2) / v_batches_per_epoch
                    train_cost, sen, spe, f = 0.0, 0.0, 0.0, 0.0
                # after training, return accuracy of validation set
            return final_train, final_test





            # logger.info("Training finished!!!")
            # for testing
            # train_Label_Error_Rate, sen, spe, f = 0.0, 0.0, 0.0, 0.0
            #
            # n_batches = int(self.NUM_TEST / self.BATCH_SIZE)
            # epoch_start = time.time()
            # sess.run(test_iterator.initializer)
            # # logger.info(section.format('Testing data'))
            # for _ in range(int(n_batches)):
            #     se, sp, tempf1 = sess.run([sensitivity, specificity, f1], feed_dict={handle: test_handle})
            #     sen = sen + se
            #     spe = spe + sp
            #     f = f + tempf1
            # # epoch_duration = time.time() - epoch_start
            # # logger.info(
            # #     '''Test_accuracy: {:.3f},Sensitivity: {:.3f},Specificity: {:.3f},F1-score: {:.3f},time: {:.2f} sec'''
            # #     .format(((sen + spe) / 2) / n_batches,
            # #             sen / n_batches,
            # #             spe / n_batches,
            # #             f / n_batches,
            # #             epoch_duration))


hyperparameters = HyperParameters()

if __name__ == "__main__":
    hyperparameters.main()