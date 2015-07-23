from __future__ import print_function
from collections import Counter
import cPickle as pickle
from datetime import datetime
import pprint
from time import time
import sys

import pandas as pd
import numpy as np

import lasagne
import lasagne.layers
from lasagne import init

from lasagne.updates import *
from lasagne import updates
from lasagne.objectives import (MaskedObjective, Objective,
                                categorical_crossentropy, mse)
from lasagne.layers import get_output, InputLayer
from nolearn.lasagne import NeuralNet, BatchIterator
from nolearn.lasagne.handlers import SaveWeights
import theano
from theano import tensor as T

from definitions import *
from quadratic_weighted_kappa import quadratic_weighted_kappa
import util
from iterator import SingleIterator

def create_net(model, tta=False, retrain_until=None, **kwargs):
    args = {
        'layers': model.layers,
        'batch_iterator_train': SingleIterator(
            model, batch_size=model.get('batch_size_train', BATCH_SIZE),
            deterministic=False, resample=model.get('resample', True)),
        'batch_iterator_test': SingleIterator(
            model, batch_size=model.get('batch_size_test', BATCH_SIZE), 
            deterministic=False if tta else True, 
            resample=False),
        'on_epoch_finished': [
            #AdjustVariable('update_momentum', 
            #                start=model.get('momentum', INITIAL_MOMENTUM),
            #                stop=0.999),
            DecayReluSlope(),
            SaveWeights(model.weights_epoch, every_n_epochs=5),
            SaveBestWeights(
                loss='train_loss' if retrain_until is not None else 'kappa',
                greater_is_better=False if retrain_until is not None else True,
            ),
        ],
        'objective': get_l2_objective(model.get('weight_decay', 0.0005)),
        'use_label_encoder': False,
        'eval_size': 0.1,
        'regression': model.get('regression', REGRESSION),
        'max_epochs': MAX_ITER,
        'verbose': 2,
        'update_learning_rate': theano.shared(
            float32(model.get('schedule')[0])),
    }
    if model.get('update') == 'adam':
        args['update'] = adam
    else:
        args['update'] = nesterov_momentum
        args['update_momentum'] = theano.shared(float32(INITIAL_MOMENTUM))

    if retrain_until is not None:
        args['eval_size'] = 0.0
        loss = 'train_loss'
        save_after_epoch = False
        args['on_epoch_finished'].append(RetrainUntil(threshold=retrain_until))
    else:
        loss = CUSTOM_SCORE_NAME
        save_after_epoch = True
        args['custom_score'] = (CUSTOM_SCORE_NAME, util.kappa)
        args['on_epoch_finished'] += [
            Schedule('update_learning_rate', model.get('schedule')),
            SaveWeights(model.weights_best, every_n_epochs=1, only_best=True),
        ]

    args.update(kwargs)
    net = Net(**args)
    net._model = model

    return net


def float32(k):
    return np.cast['float32'](k)


#def layer_std(layer, include_biases=False):
#    if include_biases:
#        all_params = lasagne.layers.get_all_params(layer)
#    else:
#        all_params = lasagne.layers.get_all_non_bias_params(layer)
#
#    return sum((T.std(p) / T.max(abs(p)) for p in all_params))


def get_l2_objective(alpha=0.0005):
    class RegularizedObjective(Objective):

        def get_loss(self, input=None, target=None, aggregation=None,
                     deterministic=False, **kwargs):

            loss = super(RegularizedObjective, self).get_loss(
                input=input, target=target, aggregation=aggregation,
                deterministic=deterministic, **kwargs)
            if not deterministic:
                return loss \
                    + alpha * lasagne.regularization.regularize_network_params(
                        self.input_layer, lasagne.regularization.l2)
            else:
                return loss
    return RegularizedObjective


def get_l1_objective(l1=0.0005):
    class RegularizedObjective(Objective):

        def get_loss(self, input=None, target=None, aggregation=None,
                     deterministic=False, **kwargs):

            loss = super(RegularizedObjective, self).get_loss(
                input=input, target=target, aggregation=aggregation,
                deterministic=deterministic, **kwargs)
            if not deterministic:
                return loss \
                    + l1 * lasagne.regularization.regularize_network_params(
                        self.input_layer, lasagne.regularization.l1)
            else:
                return loss
    return RegularizedObjective


class AdjustVariable(object):
    def __init__(self, name, start=0.03, stop=0.001):
        self.name = name
        self.start, self.stop = start, stop
        self.ls = None

    def __call__(self, nn, train_history):
        if self.ls is None:
            self.ls = np.linspace(self.start, self.stop, nn.max_epochs)

        epoch = train_history[-1]['epoch']
        new_value = float32(self.ls[epoch - 1])
        getattr(nn, self.name).set_value(new_value)


class DecayReluSlope(object):
    def __call__(self, nn, train_history):
        slope = nn._model.get('slope', None)
        value = None
        if slope is not None and 'slope_schedule' in nn._model.cnf:
            schedule = nn._model.cnf['slope_schedule']
            epoch = train_history[-1]['epoch']
            if epoch in schedule:
                value = float32(schedule[epoch])
        elif slope is not None:
            value = float32(
                max(slope.get_value() * nn._model.cnf['slope_decay'],
                    nn._model.get('min_slope', 0.0)))
        if value:
            slope.set_value(value)


class Schedule(object):
    def __init__(self, name, schedule):
        self.name = name
        self.schedule = schedule

    def __call__(self, nn, train_history):
        epoch = train_history[-1]['epoch']
        if epoch in self.schedule:
            new_value = self.schedule[epoch]
            if new_value == 'stop':
                if hasattr(nn, '_model'):
                    nn.save_params_to(nn._model.final_weights_file)
                raise StopIteration
            getattr(nn, self.name).set_value(float32(new_value))


class SaveBestWeights(object):
    def __init__(self, loss='kappa', greater_is_better=True):
        self.best_valid = np.inf
        self.best_valid_epoch = 0
        self.best_weights = None
        self.loss = loss
        self.greater_is_better = greater_is_better

    def __call__(self, nn, train_history):
        current_valid = train_history[-1][self.loss] \
            * (-1.0 if self.greater_is_better else 1.0)
        current_epoch = train_history[-1]['epoch']
        if current_valid < self.best_valid:
            self.best_valid = current_valid
            self.best_valid_epoch = current_epoch
            self.best_weights = [w.get_value() for w in nn.get_all_params()]
            nn.save_params_to(nn._model.weights_file)


class LossThreshold(object):
    """Stop when loss reaches a certain threshold (when retraining)."""
    def __init__(self, threshold, loss='train_loss'):
        self.threshold = threshold
        self.loss = loss

    def __call__(self, nn, train_history):
        current_loss = train_history[-1][self.loss]
        if current_loss < self.threshold:
            print("Train loss threshold {} reached. Stopping."
                  "".format(self.threshold))
            raise StopIteration


class RetrainUntil(LossThreshold):
    def __call__(self, nn, train_history):
        nn.save_params_to(nn._model.retrain_weights_file)
        super(RetrainUntil, self).__call__(nn, train_history)

#class QueueIterator(BatchIterator):
#    """BatchIterator with seperate thread to do the image reading."""
#    def __iter__(self):
#        queue = Queue.Queue(maxsize=20)
#        end_marker = object()
#
#        def producer():
#            for Xb, yb in super(QueueIterator, self).__iter__():
#                queue.put((np.array(Xb), np.array(yb)))
#            queue.put(end_marker)
#
#        thread = threading.Thread(target=producer)
#        thread.daemon = True
#        thread.start()
#
#        item = queue.get()
#        while item is not end_marker:
#            yield item
#            queue.task_done()
#            item = queue.get()


class Net(NeuralNet):

    def load_params_from(self, source):
        self.initialize()

        if isinstance(source, str):
            with open(source, 'rb') as f:
                source = pickle.load(f)

        if isinstance(source, NeuralNet):
            source = source.get_all_params_values()

        success = "loaded parameters to layer '{}' (shape {})."
        failure = ("Could not load parameters to layer '{}' because "
                   "shapes did not match: {} vs {}.")
        partially = ("Partially loaded parameters to layer '{}' because "
                     "shapes did not match: {} vs {}.")

        for key, values in source.items():
            layer = self.layers_.get(key)
            if layer is not None:
                for p1, p2v in zip(layer.get_params(), values):
                    shape1 = p1.get_value().shape
                    shape2 = p2v.shape
                    shape1s = 'x'.join(map(str, shape1))
                    shape2s = 'x'.join(map(str, shape2))
                    if shape1 == shape2:
                        p1.set_value(p2v)
                        if self.verbose:
                            print(success.format(
                                key, shape1s, shape2s))
                    elif shape1[2:] == shape2[2:]:
                        # only works if more filters are being added
                        part = p1.get_value()
                        if len(shape2) == 4:
                            if shape1 > shape2:
                                part[:shape2[0], :shape2[1]] = p2v
                            else:
                                part[:] = p2v[:shape1[0], :shape1[1]]
                        elif len(shape2) == 3:
                            part[:shape2[0]] = p2v
                        else:
                            continue
                        p1.set_value(part)
                        if self.verbose:
                            print(partially.format(key, shape1s, shape2s))
                    else:
                        if self.verbose:
                            print(failure.format(
                                key, shape1s, shape2s))


    def train_test_split(self, X, y, eval_size):
        if eval_size:
            X_train, X_valid, y_train, y_valid = util.split(X, y,
                                                            test_size=eval_size)
        else:
            X_train, y_train = X, y
            X_valid, y_valid = X[len(X):], y[len(y):]

        return X_train, X_valid, y_train, y_valid

    def initialize(self):
        if getattr(self, '_initialized', False):
            return

        out = getattr(self, '_output_layer', None)
        if out is None:
            out = self._output_layer = self.initialize_layers()
        self._check_for_unused_kwargs()

        iter_funcs = self._create_iter_funcs(
            self.layers_, self.objective, self.update,
            self.y_tensor_type,
            )
        self.train_iter_, self.eval_iter_, self.predict_iter_, self.transform_iter_ = iter_funcs
        self._initialized = True

    def _create_iter_funcs(self, layers, objective, update, output_type):
        y_batch = output_type('y_batch')

        output_layer = list(layers.values())[-1]
        objective_params = self._get_params_for('objective')
        obj = objective(output_layer, **objective_params)
        if not hasattr(obj, 'layers'):
            # XXX breaking the Lasagne interface a little:
            obj.layers = layers

        loss_train = obj.get_loss(None, y_batch)
        loss_eval = obj.get_loss(None, y_batch, deterministic=True)
        predict_proba = get_output(output_layer, None, deterministic=True)

        try:
            transform = get_output([v for k, v in layers.items() 
                                   if 'rmspool' in k or 'maxpool' in k][-1],
                                   None, deterministic=True)
        except IndexError:
            transform = get_output(layers.values()[-2], None,
                                   deterministic=True)
        #transform = get_output(layers.values()[-2], None,
        #                       deterministic=True)

        if not self.regression:
            predict = predict_proba.argmax(axis=1)
            accuracy = T.mean(T.eq(predict, y_batch))
        else:
            accuracy = loss_eval

        all_params = self.get_all_params(trainable=True)
        update_params = self._get_params_for('update')
        updates = update(loss_train, all_params, **update_params)

        input_layers = [layer for layer in layers.values()
                        if isinstance(layer, InputLayer)]

        X_inputs = [theano.Param(input_layer.input_var, name=input_layer.name)
                    for input_layer in input_layers]
        inputs = X_inputs + [theano.Param(y_batch, name="y")]

        train_iter = theano.function(
            inputs=inputs,
            outputs=[loss_train],
            updates=updates,
            )
        eval_iter = theano.function(
            inputs=inputs,
            outputs=[loss_eval, accuracy],
            )
        predict_iter = theano.function(
            inputs=X_inputs,
            outputs=predict_proba,
            )
        transform_iter = theano.function(
            inputs=X_inputs,
            outputs=transform,
            )
        return train_iter, eval_iter, predict_iter, transform_iter


#    def _create_iter_funcs(self, layers, objective, update, input_type,
#                           output_type):
#        X = input_type('x')
#        y = output_type('y')
#        X_batch = input_type('x_batch')
#        y_batch = output_type('y_batch')
#
#        output_layer = list(layers.values())[-1]
#        objective_params = self._get_params_for('objective')
#        obj = objective(output_layer, **objective_params)
#        if not hasattr(obj, 'layers'):
#            # XXX breaking the Lasagne interface a little:
#            obj.layers = layers
#
#        loss_train = obj.get_loss(X_batch, y_batch)
#        loss_eval = obj.get_loss(X_batch, y_batch, deterministic=True)
#        predict_proba = output_layer.get_output(X_batch, deterministic=True)
#
#
#        if not self.regression:
#            predict = predict_proba.argmax(axis=1)
#            accuracy = T.mean(T.eq(predict, y_batch))
#        else:
#            accuracy = loss_eval
#
#        all_params = self.get_all_params()
#        update_params = self._get_params_for('update')
#        updates = update(loss_train, all_params, **update_params)
#
#        train_iter = theano.function(
#            inputs=[theano.Param(X_batch), theano.Param(y_batch)],
#            outputs=[loss_train],
#            updates=updates,
#            givens={
#                X: X_batch,
#                y: y_batch,
#                },
#            )
#        eval_iter = theano.function(
#            inputs=[theano.Param(X_batch), theano.Param(y_batch)],
#            outputs=[loss_eval, accuracy],
#            givens={
#                X: X_batch,
#                y: y_batch,
#                },
#            )
#        predict_iter = theano.function(
#            inputs=[theano.Param(X_batch)],
#            outputs=predict_proba,
#            givens={
#                X: X_batch,
#                },
#            )
#
#        return {
#            'train_iter_': train_iter,
#            'eval_iter_': eval_iter,
#            'predict_iter_': predict_iter,
#            'transform_iter_': transform_iter
#        }

    def _check_for_unused_kwargs(self):
        names = list(self.layers_.keys()) + ['update', 'objective', 'loss']
        for k in self._kwarg_keys:
            for n in names:
                prefix = '{}_'.format(n)
                if k.startswith(prefix):
                    break
            else:
                raise ValueError("Unused kwarg: {}".format(k))

    #def fit(self, X, y):
    #    self.objective.mask = util.get_mask(y)
    #    return super(Net, self).fit(X, y)

    def transform(self, X, transform=None, color_vec=None):

        features = []
        for Xb, yb in self.batch_iterator_test(X, transform=transform,
                                               color_vec=color_vec):
            # add dummy data for nervana kernels that need batch_size % 8 = 0
            missing = (8 - len(Xb) % 8) % 8
            if missing != 0:
                tiles = np.ceil(float(missing) / len(Xb)).astype(int) + 1
                Xb = np.tile(Xb, [tiles] + [1] * (Xb.ndim - 1))\
                        [:len(Xb) + missing]

            transforms = self.transform_iter_(Xb)

            if missing != 0:
                transforms = transforms[:-missing]
            
            features.append(transforms)

        return np.vstack(features)
    
    def predict_proba(self, X):
        probas = []
        for Xb, yb in self.batch_iterator_test(X):

            # add dummy data for nervana kernels that need batch_size % 8 = 0
            missing = (8 - len(Xb) % 8) % 8
            if missing != 0:
                tiles = np.ceil(float(missing) / len(Xb)).astype(int) + 1
                Xb = np.tile(Xb, [tiles] + [1] * (Xb.ndim - 1))\
                        [:len(Xb) + missing]

            preds = self.predict_iter_(Xb)

            if missing != 0:
                preds = preds[:-missing]

            probas.append(preds)

        return np.vstack(probas)

    def predict(self, X):
        if self.regression:
            return self.predict_proba(X)
        else:
            y_pred = np.argmax(self.predict_proba(X), axis=1)
            if self.use_label_encoder:
                y_pred = self.enc_.inverse_transform(y_pred)
            return y_pred

    def train_loop(self, X, y):
        X_train, X_valid, y_train, y_valid = self.train_test_split(
            X, y, self.eval_size)

        on_epoch_finished = self.on_epoch_finished
        if not isinstance(on_epoch_finished, (list, tuple)):
            on_epoch_finished = [on_epoch_finished]

        on_training_started = self.on_training_started
        if not isinstance(on_training_started, (list, tuple)):
            on_training_started = [on_training_started]

        on_training_finished = self.on_training_finished
        if not isinstance(on_training_finished, (list, tuple)):
            on_training_finished = [on_training_finished]

        epoch = 0
        best_valid_loss = (
            min([row['valid_loss'] for row in self.train_history_]) if
            self.train_history_ else np.inf
            )
        best_train_loss = (
            min([row['train_loss'] for row in self.train_history_]) if
            self.train_history_ else np.inf
            )
        for func in on_training_started:
            func(self, self.train_history_)

        num_epochs_past = len(self.train_history_)

        class_losses = None

        while epoch < self.max_epochs:
            epoch += 1

            train_losses = []
            valid_losses = []
            valid_accuracies = []
            #custom_score = []
            y_pred, y_true = [], []

            t0 = time()
            toc = time()

            for Xb, yb in self.batch_iterator_train(X_train, y_train):
                batch_train_loss = self.train_iter_(Xb, yb)
                #print(batch_train_loss)

                if not np.isfinite(batch_train_loss[0]):
                    raise ValueError("non finite loss")

                #if epoch == 1:
                #    print('train loss {}'.format(batch_train_loss[0]))

                #print('iter took {:.4f} s'.format(time() - toc))
                toc = time()
                train_losses.append(batch_train_loss)

            #loss_per_class = []
            for Xb, yb in self.batch_iterator_test(X_valid, y_valid):

                batch_valid_loss, accuracy = self.eval_iter_(Xb, yb)
                #class_dist = dict(Counter(yb.flatten()))
                #class_dist['loss'] = batch_valid_loss
                #print(class_dist)
                #loss_per_class.append(class_dist)

                valid_losses.append(batch_valid_loss)
                valid_accuracies.append(accuracy)
                y_true.append(yb)
                if self.custom_score:
                    y_prob = self.predict_iter_(Xb)
                    y_pred.append(y_prob)
                    #custom_score.append(self.custom_score[1](yb, y_prob))

            #df = pd.DataFrame(loss_per_class)
            #df.to_pickle('loss.df')
            #exit(0)

            avg_train_loss = np.mean(train_losses)
            avg_valid_loss = np.mean(valid_losses)
            avg_valid_accuracy = np.mean(valid_accuracies)
            if self.custom_score and self.eval_size:
                #avg_custom_score = np.mean(custom_score)

                y_true = np.concatenate(y_true)
                y_pred = np.concatenate(y_pred)

                y_pred = np.clip(y_pred, np.min(y_true), np.max(y_true))

                #from sklearn.metrics import confusion_matrix
                #print(confusion_matrix(y_true, np.round(y_pred).astype(int)))

                #if self._model.get('ordinal'):
                #    y_pred = np.sum(y_pred, axis=1)

                avg_custom_score = self.custom_score[1](y_true, y_pred)

            if avg_train_loss < best_train_loss:
                best_train_loss = avg_train_loss
            if avg_valid_loss < best_valid_loss:
                best_valid_loss = avg_valid_loss

            info = {
                'epoch': num_epochs_past + epoch,
                'train_loss': avg_train_loss,
                'train_loss_best': best_train_loss == avg_train_loss,
                'valid_loss': avg_valid_loss,
                'valid_loss_best': best_valid_loss == avg_valid_loss,
                'valid_accuracy': avg_valid_accuracy,
                'dur': time() - t0,
                }
            if self.custom_score and self.eval_size:
                info[self.custom_score[0]] = avg_custom_score
            self.train_history_.append(info)

            try:
                for func in on_epoch_finished:
                    func(self, self.train_history_)
            except StopIteration:
                break

        for func in on_training_finished:
            func(self, self.train_history_)

#class MyMasked(MaskedObjective):
#    def get_loss(self, input=None, target=None, mask=None,
#                 aggregation=None, deterministic=False, **kwargs):
#        """
#        Get loss scalar expression
#
#        :parameters:
#            - input : (default `None`) an expression that results in the
#                input data that is passed to the network
#            - target : (default `None`) an expression that results in the
#                desired output that the network is being trained to generate
#                given the input
#            - mask : None for no mask, or a soft mask that is the same shape
#                as - or broadcast-able to the shape of - the result of
#                applying the loss function. It selects/weights the
#                contributions of the resulting loss values
#            - aggregation : None to use the value passed to the
#                constructor or a value to override it
#            - kwargs : additional keyword arguments passed to `input_layer`'s
#                `get_output` method
#
#        :returns:
#            - output : loss expressions
#        """
#        print(input, input.shape)
#        print(target, target.shape)
#        print(mask, mask.shape)
#        print(np.unique(mask))
#
#        network_output = get_output(self.input_layer, input, **kwargs)
#        if target is None:
#            target = self.target_var
#        if mask is None:
#            mask = self.mask_var
#
#        if aggregation not in self._valid_aggregation:
#            raise ValueError('aggregation must be \'mean\', \'sum\', '
#                             '\'normalized_sum\' or None, '
#                             'not {0}'.format(aggregation))
#
#        # Get aggregation value passed to constructor if None
#        if aggregation is None:
#            aggregation = self.aggregation
#
#        if deterministic:
#            masked_losses = self.loss_function(network_output, target)
#        else:
#            masked_losses = self.loss_function(network_output, target) \
#                    * mask_from_labels(target)
#
#        if aggregation is None or aggregation == 'mean':
#            return masked_losses.mean()
#        elif aggregation == 'sum':
#            return masked_losses.sum()
#        elif aggregation == 'normalized_sum':
#            return masked_losses.sum() / mask.sum()
#        else:
#            raise RuntimeError('This should have been caught earlier')
#
