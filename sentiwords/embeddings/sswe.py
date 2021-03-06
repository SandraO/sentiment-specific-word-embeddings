"""Module for the sentiment specific word embedding model.
Includes custom input generator, model and commandline interface.
author: Maurice Gerczuk
"""
import csv
import tensorflow as tf
import argparse
from ..processing.preprocessing import Preprocessor
from .embedding import load_vocab, lookup_ids, Embedding
from nltk import ngrams
from random import randint
from os.path import join
from os import listdir

# Set logging verbosity to INFO so that loss is printed to cmd during training.
tf.logging.set_verbosity(tf.logging.INFO)


class InputGenerator():
    """Input Generator for the SSWE model.
    Extracts non-overlapping trigrams (of ngrams) from a given tweet csv.
    It yields the vocabulary ids of this original tri-gram, those of a
    corresponding corrupted tri-gram (the center word is replaced with a
    random one) and a sentiment label.
    """

    def __init__(
            self,
            input_path,
            vocab,
            preprocessor=Preprocessor(),
            csv_delimiter=',',
            ngram=1,
            csv_columns=['sentiment', 'id', 'date', 'status', 'user', 'text']):
        """Initialize the InputGenerator.
        arguments:
           input_path: Path to input csv.
           vocab: Vocabulary to look up word ids.
           preprocessor: Preprocessor object for tokenizing input data.
           csv_delimiter: Delimiter used in the input_csv.
           ngram: Degree of ngram to be extracted from input data tweets.
           csv_columns: columns for the input data. 'text' column should
                        contain the raw tweets and 'sentiment' column the
                        tweet polarity labels.
        """

        input_file = open(input_path, 'r', encoding='utf-8')
        self.reader = csv.reader(input_file, delimiter=csv_delimiter)
        if csv_columns is None:
            self.csv_columns = next(self.reader)
        self.preprocessor = preprocessor
        self.ngram = ngram
        self.csv_columns = csv_columns
        self.vocab = vocab
        self.samples = iter(self.generate_samples())

    def generate_samples(self):
        """Generate input samples of ((original tri-gram, corrupted tri-gram),
        label). Samples are extracted from the tweets in a non-overlapping fashion.

        yields: A tuple of ((original_trigram, corrupted_trigram), label).
        """
        for line in self.reader:

            # extract a list of non-overlapping ngrams from the tweet
            current_tweet = list(
                map('_'.join,
                    ngrams(
                        self.preprocessor.tokenize_tweet(
                            line[self.csv_columns.index('text')]),
                        self.ngram)))[::self.ngram]

            # sentiment labels > 0 are considered positive (1) others negative
            current_label = 1 if int(
                line[self.csv_columns.index('sentiment')]) > 0 else -1

            # group three neighboring ngrams together (without overlap)
            chunks = (current_tweet[i * 3:(i + 1) * 3]
                      for i in range(0, int(len(current_tweet) / 3)))
            for current_chunk in chunks:
                sample = lookup_ids(self.vocab, current_chunk)
                yield (sample, self.corrupted_sample(sample)), current_label

    def corrupted_sample(self, sample):
        """Generate a corrupted sample from an input tri-gram. The middle word
        is replaced by a random one.
        arguments:
           sample: Input trigram.
        returns: Corrupted trigram.
        """
        random_word = randint(0, len(self.vocab) - 1)
        while random_word == sample[1]:
            random_word = randint(0, len(self.vocab) - 1)
        return [sample[0], random_word, sample[2]]

    def __iter__(self):
        """Return an iterator (which objects of this class are)."""
        return self

    def __next__(self):
        """Standard method for iterators: Return next element."""
        return next(self.samples)


def input_fn(csv,
             vocabulary,
             csv_delimiter=',',
             preprocessor=Preprocessor(),
             ngram=1,
             shuffle=True,
             num_epochs=None,
             batch_size=32,
             csv_columns=['sentiment', 'id', 'date', 'status', 'user',
                          'text']):
    """input_fn created from InputGenerator to be used with tf.Estimator.
    arguments:
       csv: Path to input csv.
       vocab: Vocabulary to look up word ids.
       csv_delimiter: Delimiter used in the input_csv.
       preprocessor: Preprocessor object for tokenizing input data.
       ngram: Degree of ngram to be extracted from input data tweets.
       shuffle: Whether input data should be shuffled.
       num_epochs: How many times the input_fn should cycle through the data.
       batch_size: How many elements to generate for one step.
       csv_columns: columns for the input data. 'text' column should
                    contain the raw tweets and 'sentiment' column the
                    tweet polarity labels.
    returns: A tuple of features, labels where features is a dict containing
             both the original ngrams (key: 'original) and the corresponding
             corrupted ngrams (key: 'corrupted).
    """
    input_generator = InputGenerator(csv, vocabulary, preprocessor,
                                     csv_delimiter, ngram, csv_columns)

    def gen():
        for sample in input_generator:
            yield sample

    dataset = tf.data.Dataset.from_generator(
        gen, output_types=(tf.int64, tf.int64), output_shapes=([2, 3], []))
    if shuffle:
        dataset = dataset.shuffle(buffer_size=10000)
    dataset = dataset.repeat(num_epochs)
    dataset = dataset.batch(batch_size)
    iterator = dataset.make_one_shot_iterator()
    with tf.name_scope('input'):
        features, labels = iterator.get_next()
        return {
            'original': features[:, 0],
            'corrupted': features[:, 1]
        }, labels


def model_fn(mode,
             features,
             labels,
             vocabulary_size=None,
             alpha=0.5,
             hidden_units=20,
             learning_rate=0.1,
             embedding_size=50,
             initial_embedding=None):
    """Model function for the sswe network.
    arguments:
       mode: A tf.estimator.ModeKey. One of TRAIN, EVAL, or PREDICT.
       features: input features (dict of 'feature_name: tensor')
       labels: sentiment labels for the input features.
       vocabulary_size: Size of the embedding voaculary.
       alpha: parameter that weighs syntactic against sentiment loss.
              alpha==1 -> only syntactic loss, alpha==0 -> only
              sentiment loss.
       hidden_units: Number of units on hidden fully connected layer.
       learning_rate: Learning rate for AdagradOptimizer.
       embedding_size: Dimensionality of the used embedding.
       initial_embedding: Initial embedding matrix in numpy format.
    returns:
       an EstimatorSpec for creating a tensorflow estimator.
    """

    if initial_embedding is not None:
        init = tf.constant_initializer(initial_embedding)
    else:
        init = None

    def shared_network(input):
        """The shared part of the network. Both original and corrupted ngram
        are passed through here.
        arguments:
           input: Input feature tensor.
        returns: Output of the network (syntactic and sentiment score).
        """

        # define embedding variable
        word_embeddings = tf.get_variable(
            'word_embeddings', [vocabulary_size, embedding_size],
            initializer=init)

        # lookup embeddings for true and negative sample
        embeds = tf.nn.embedding_lookup(
            word_embeddings, input, name='embeddings')
        flattened_embeds = tf.layers.flatten(embeds, name='flattened_embeds')

        hidden = tf.layers.dense(
            flattened_embeds,
            hidden_units,
            activation=
            lambda x: tf.clip_by_value(x, clip_value_min=-1, clip_value_max=1),
            name='hidden',
            reuse=tf.AUTO_REUSE)
        output = tf.layers.dense(hidden, 2, name='output', reuse=tf.AUTO_REUSE)
        return output

    with tf.variable_scope('shared_network', reuse=tf.AUTO_REUSE) as scope:
        # original ngram output
        original_output = shared_network(features['original'])
        original_syntactic_score = original_output[:, 0]
        original_sentiment_score = original_output[:, 1]

        # corrupted ngram output
        corrupted_output = shared_network(features['corrupted'])
        corrupted_syntactic_score = corrupted_output[:, 0]
        corrupted_sentiment_score = corrupted_output[:, 1]

    if mode == tf.estimator.ModeKeys.TRAIN or mode == tf.estimator.ModeKeys.EVAL:
        with tf.name_scope('loss'):
            sentiment_loss = tf.reduce_mean(
                tf.maximum(
                    tf.cast(0, tf.float32),
                    1 - tf.cast(labels, tf.float32) * original_sentiment_score
                    + tf.cast(labels, tf.float32) * corrupted_sentiment_score),
                name='sentiment')
            tf.summary.scalar('sentiment_loss', sentiment_loss)
            syntactic_loss = tf.reduce_mean(
                tf.maximum(
                    tf.cast(0, tf.float32),
                    1 - original_syntactic_score + corrupted_syntactic_score),
                name='syntactic')
            tf.summary.scalar('syntactic_loss', syntactic_loss)
            loss = tf.add(
                (alpha * syntactic_loss), ((1 - alpha) * sentiment_loss),
                name='combined')
    else:
        loss = None

    if mode == tf.estimator.ModeKeys.PREDICT:
        predictions = original_output
    else:
        predictions = None

    if mode == tf.estimator.ModeKeys.TRAIN:
        opt = tf.train.AdagradOptimizer(learning_rate)
        train_op = opt.minimize(
            loss, global_step=tf.train.get_global_step(), name='train_op')
    else:
        train_op = None

    return tf.estimator.EstimatorSpec(mode, predictions, loss, train_op)


def main():
    """Create an input parser and then parse the arguments. Setup
    training and model parameters and train model accordingly.
    """
    parser = argparse.ArgumentParser(
        description=
        'Train Sentiment-Specific word embeddings on a csv twitter sentiment dataset.'
    )
    parser.add_argument(
        '-data',
        required=True,
        default=None,
        help='Twitter sentiment dataset in csv format.')
    parser.add_argument(
        '--vocabulary',
        help='Vocabulary file (each word on separate line).',
        default=None)
    parser.add_argument(
        '--batch_size', default=32, type=int, help='Batchsize for training.')
    parser.add_argument(
        '--epochs',
        default=10,
        type=int,
        help='Number of epochs to train the model.')
    parser.add_argument(
        '--model_dir',
        default=None,
        help=
        'Directory for saving and restoring model checkpoints, summaries and exports.'
    )
    parser.add_argument(
        '--alpha',
        default=0.5,
        type=float,
        help=
        'Alpha parameter used to weigh syntactic versus sentiment loss. 1 means only sysntactic loss is considered while 0 uses only semantic loss.'
    )
    parser.add_argument('--lr', default=0.1, type=float, help='Learning rate.')
    parser.add_argument(
        '--hidden',
        default=20,
        type=int,
        help='Number of units of the hidden layer.')
    parser.add_argument(
        '--embedding_size',
        default=25,
        type=int,
        help='Size of word embedding vectors.')
    parser.add_argument(
        '--keep_checkpoints',
        default=5,
        type=int,
        help='How many checkpoints to keep stored on disk.')
    parser.add_argument(
        '--initial_embeddings',
        default=None,
        help='Initialize the embedding matrix from a csv file.')
    parser.add_argument(
        '--export_path', default=None, help='Export path to embedding csv.')
    args = parser.parse_args()

    # Initialize embedding from training data.
    if args.vocabulary is None and args.initial_embeddings is None:
        print('Building vocabulary from input data...')
        processor = Preprocessor()
        sentences = processor.preprocess_csv(args.data)
        embedding = Embedding(args.embedding_size)
        embedding._build_vocabulary(sentences)
        vocab = embedding.vocabulary
        embedding_matrix = None
    # Load initial embeddings from file.
    elif args.initial_embeddings is not None:
        print('Loading embeddings from file...')
        embedding = Embedding()
        embedding.load(args.initial_embeddings)
        vocab = embedding.vocabulary
        embedding_matrix = embedding.embedding_matrix
    # Load given vocabulary.
    else:
        print('Loading vocabulary...')
        vocab = load_vocab(args.vocabulary)
        embedding_matrix = None

    if embedding_matrix is not None:
        embedding_size = embedding_matrix.shape[1]
    else:
        embedding_size = args.embedding_size

    # setup session configuration. Allow gpu growth (do not use all available memory)
    gpu_options = tf.GPUOptions(allow_growth=True)
    session_config = tf.ConfigProto(gpu_options=gpu_options)
    config = tf.estimator.RunConfig(
        model_dir=args.model_dir,
        keep_checkpoint_max=args.keep_checkpoints,
        session_config=session_config)

    # setup model (with tf.Estimator framework)
    model_function = lambda mode, features, labels: model_fn(mode=mode,
                                                             features=features, labels=labels,
                                                             vocabulary_size=len(vocab),
                                                             alpha=args.alpha,
                                                             hidden_units=args.hidden,
                                                             learning_rate=args.lr,
                                                             embedding_size=embedding_size,
                                                             initial_embedding=embedding_matrix)
    model = tf.estimator.Estimator(
        model_fn=model_function, model_dir=args.model_dir, config=config)
    model_dir = model.model_dir

    # start training
    model.train(lambda: input_fn(args.data, vocab, num_epochs=args.epochs, batch_size=args.batch_size))

    # export the embedding as csv
    if args.export_path is not None:
        graph_path = [
            join(model_dir, meta_graph) for meta_graph in listdir(model_dir)
            if meta_graph.endswith('.meta')
        ][0]
        with tf.Session() as sess:
            saver = tf.train.import_meta_graph(graph_path)
            saver.restore(sess, tf.train.latest_checkpoint(model_dir))
            embedding_matrix = sess.run('shared_network/word_embeddings:0')
        embedding = Embedding(size=args.embedding_size)
        embedding.embedding_matrix = embedding_matrix
        embedding.vocabulary = vocab
        embedding.save(args.export_path)


if __name__ == '__main__':
    main()
