import logging
from timeit import default_timer as timer

from config import load_parameters
from data_engine.prepare_data import build_dataset
from viddesc_model import VideoDesc_Model

from keras_wrapper.cnn_model import loadModel, saveModel
from keras_wrapper.extra.callbacks import PrintPerformanceMetricOnEpochEnd, PrintPerformanceMetricEachNUpdates, SampleEachNUpdates
from keras_wrapper.extra.read_write import dict2pkl, list2file

import sys
import ast
logging.basicConfig(level=logging.DEBUG, format='[%(asctime)s] %(message)s', datefmt='%d/%m/%Y %H:%M:%S')
logger = logging.getLogger(__name__)


def train_model(params):
    """
    Training function. Sets the training parameters from params. Build or loads the model and launches the training.
    :param params: Dictionary of network hyperparameters.
    :return: None
    """

    if params['RELOAD'] > 0:
        logging.info('Resuming training.')

    check_params(params)

    # Load data
    dataset = build_dataset(params)
    params['OUTPUT_VOCABULARY_SIZE'] = dataset.vocabulary_len[params['OUTPUTS_IDS_DATASET'][0]]

    # Build model
    if(params['RELOAD'] == 0): # build new model 
        video_model = VideoDesc_Model(params,
                                      type=params['MODEL_TYPE'],
                                      verbose=params['VERBOSE'],
                                      model_name=params['MODEL_NAME'],
                                      vocabularies=dataset.vocabulary,
                                      store_path=params['STORE_PATH'])
        dict2pkl(params, params['STORE_PATH'] + '/config')

        # Define the inputs and outputs mapping from our Dataset instance to our model
        inputMapping = dict()
        for i, id_in in enumerate(params['INPUTS_IDS_DATASET']):
            if len(video_model.ids_inputs) > i:
                pos_source = dataset.ids_inputs.index(id_in)
                id_dest = video_model.ids_inputs[i]
                inputMapping[id_dest] = pos_source
        video_model.setInputsMapping(inputMapping)
            
        outputMapping = dict()
        for i, id_out in enumerate(params['OUTPUTS_IDS_DATASET']):
            if len(video_model.ids_outputs) > i:
                pos_target = dataset.ids_outputs.index(id_out)
                id_dest = video_model.ids_outputs[i]
                outputMapping[id_dest] = pos_target
        video_model.setOutputsMapping(outputMapping)
        
    else: # resume from previously trained model
        video_model = loadModel(params['STORE_PATH'], params['RELOAD'])
        video_model.setOptimizer()
    ###########

    
    ########### Callbacks
    callbacks = buildCallbacks(params, video_model, dataset)
    ###########


    ########### Training
    total_start_time = timer()

    logger.debug('Starting training!')
    training_params = {'n_epochs': params['MAX_EPOCH'], 'batch_size': params['BATCH_SIZE'],
                       'homogeneous_batches': params['HOMOGENEOUS_BATCHES'], 'maxlen': params['MAX_OUTPUT_TEXT_LEN'],
                       'lr_decay': params['LR_DECAY'], 'lr_gamma': params['LR_GAMMA'],
                       'epochs_for_save': params['EPOCHS_FOR_SAVE'], 'verbose': params['VERBOSE'],
                       'eval_on_sets': params['EVAL_ON_SETS_KERAS'], 'n_parallel_loaders': params['PARALLEL_LOADERS'],
                       'extra_callbacks': callbacks, 'reload_epoch': params['RELOAD'], 'epoch_offset': params['RELOAD'],
                       'data_augmentation': params['DATA_AUGMENTATION'],
                       'patience': params.get('PATIENCE', 0), 'metric_check': params.get('STOP_METRIC', None)}
    video_model.trainNet(dataset, training_params)

    total_end_time = timer()
    time_difference = total_end_time - total_start_time
    logging.info('In total is {0:.2f}s = {1:.2f}m'.format(time_difference, time_difference / 60.0))


def apply_Video_model(params):
    """
        Function for using a previously trained model for sampling.
    """
    
    ########### Load data
    dataset = build_dataset(params)
    params['OUTPUT_VOCABULARY_SIZE'] = dataset.vocabulary_len[params['OUTPUTS_IDS_DATASET'][0]]
    ###########
    
    
    ########### Load model
    video_model = loadModel(params['STORE_PATH'], params['RELOAD'])
    video_model.setOptimizer()
    ###########
    

    ########### Apply sampling
    extra_vars = dict()
    extra_vars['tokenize_f'] = eval('dataset.' + params['TOKENIZATION_METHOD'])
    extra_vars['language'] = params.get('TRG_LAN', 'en')

    for s in params["EVAL_ON_SETS"]:

        # Apply model predictions
        params_prediction = {'batch_size': params['BATCH_SIZE'],
                             'n_parallel_loaders': params['PARALLEL_LOADERS'],
                             'predict_on_sets': [s]}

        # Convert predictions into sentences
        vocab = dataset.vocabulary[params['OUTPUTS_IDS_DATASET'][0]]['idx2words']

        if params['BEAM_SEARCH']:
            params_prediction['beam_size'] = params['BEAM_SIZE']
            params_prediction['maxlen'] = params['MAX_OUTPUT_TEXT_LEN_TEST']
            params_prediction['optimized_search'] = params['OPTIMIZED_SEARCH']
            params_prediction['model_inputs'] = params['INPUTS_IDS_MODEL']
            params_prediction['model_outputs'] = params['OUTPUTS_IDS_MODEL']
            params_prediction['dataset_inputs'] = params['INPUTS_IDS_DATASET']
            params_prediction['dataset_outputs'] = params['OUTPUTS_IDS_DATASET']
            params_prediction['normalize'] = params['NORMALIZE_SAMPLING']

            params_prediction['alpha_factor'] = params['ALPHA_FACTOR']
            predictions = video_model.predictBeamSearchNet(dataset, params_prediction)[s]
            predictions = video_model.decode_predictions_beam_search(predictions,
                                                                     vocab,
                                                                     verbose=params['VERBOSE'])
        else:
            predictions = video_model.predictNet(dataset, params_prediction)[s]
            predictions = video_model.decode_predictions(predictions, 1, # always set temperature to 1
                                                                vocab, params['SAMPLING'], verbose=params['VERBOSE'])

        # Store result
        filepath = video_model.model_path+'/'+ s +'_sampling.pred' # results file
        if params['SAMPLING_SAVE_MODE'] == 'list':
            list2file(filepath, predictions)
        else:
            raise Exception, 'Only "list" is allowed in "SAMPLING_SAVE_MODE"'


        # Evaluate if any metric in params['METRICS']
        for metric in params['METRICS']:
            logging.info('Evaluating on metric ' + metric)
            filepath = video_model.model_path + '/' + s + '_sampling.' + metric  # results file

            # Evaluate on the chosen metric
            extra_vars[s] = dict()
            extra_vars[s]['references'] = dataset.extra_variables[s][params['OUTPUTS_IDS_DATASET'][0]]
            metrics = utils.evaluation.select[metric](
                pred_list=predictions,
                verbose=1,
                extra_vars=extra_vars,
                split=s)

            # Print results to file
            with open(filepath, 'w') as f:
                header = ''
                line = ''
                for metric_ in sorted(metrics):
                    value = metrics[metric_]
                    header += metric_ + ','
                    line += str(value) + ','
                f.write(header + '\n')
                f.write(line + '\n')
            logging.info('Done evaluating on metric ' + metric)


def buildCallbacks(params, model, dataset):
    """
    Builds the selected set of callbacks run during the training of the model.

    :param params: Dictionary of network hyperparameters.
    :param model: Model instance on which to apply the callback.
    :param dataset: Dataset instance on which to apply the callback.
    :return:
    """

    callbacks = []

    if params['METRICS']:
        # Evaluate training
        extra_vars = {'language': params.get('TRG_LAN', 'en'),
                      'n_parallel_loaders': params['PARALLEL_LOADERS'],
                      'tokenize_f': eval('dataset.' + params['TOKENIZATION_METHOD'])}
        vocab = dataset.vocabulary[params['OUTPUTS_IDS_DATASET'][0]]['idx2words']
        for s in params['EVAL_ON_SETS']:
            extra_vars[s] = dict()
            extra_vars[s]['references'] = dataset.extra_variables[s][params['OUTPUTS_IDS_DATASET'][0]]
        if params['BEAM_SIZE']:
            extra_vars['beam_size'] = params['BEAM_SIZE']
            extra_vars['maxlen'] = params['MAX_OUTPUT_TEXT_LEN_TEST']
            extra_vars['optimized_search'] = params['OPTIMIZED_SEARCH']
            extra_vars['model_inputs'] = params['INPUTS_IDS_MODEL']
            extra_vars['model_outputs'] = params['OUTPUTS_IDS_MODEL']
            extra_vars['dataset_inputs'] = params['INPUTS_IDS_DATASET']
            extra_vars['dataset_outputs'] = params['OUTPUTS_IDS_DATASET']
<<<<<<< HEAD
            extra_vars['normalize'] =  params['NORMALIZE_SAMPLING']
            extra_vars['alpha_factor'] =  params['ALPHA_FACTOR']

        if params['EVAL_EACH_EPOCHS']:
            callback_metric = PrintPerformanceMetricOnEpochEnd(model, dataset,
=======
            extra_vars['normalize'] = params['NORMALIZE_SAMPLING']
            extra_vars['alpha_factor'] = params['ALPHA_FACTOR']
            input_text_id = None
            vocab_src = None

        callback_metric = utils.callbacks.\
            PrintPerformanceMetricOnEpochEndOrEachNUpdates(model,
                                                           dataset,
>>>>>>> 2c7f6513f5b7f657afb8ebf68d177d22c0e85b39
                                                           gt_id=params['OUTPUTS_IDS_DATASET'][0],
                                                           metric_name=params['METRICS'],
                                                           set_name=params['EVAL_ON_SETS'],
                                                           batch_size=params['BATCH_SIZE'],
                                                           each_n_epochs=params['EVAL_EACH'],
                                                           extra_vars=extra_vars,
                                                           reload_epoch=params['RELOAD'],
                                                           is_text=True,
                                                           input_text_id=input_text_id,
                                                           index2word_y=vocab,
                                                           index2word_x=vocab_src,
                                                           sampling_type=params['SAMPLING'],
                                                           beam_search=params['BEAM_SEARCH'],
                                                           save_path=model.model_path,
                                                           start_eval_on_epoch=params['START_EVAL_ON_EPOCH'],
                                                           write_samples=True,
                                                           write_type=params['SAMPLING_SAVE_MODE'],
                                                           early_stop=params['EARLY_STOP'],
                                                           patience=params['PATIENCE'],
                                                           stop_metric=params['STOP_METRIC'],
                                                           eval_on_epochs=params['EVAL_EACH_EPOCHS'],
                                                           verbose=params['VERBOSE'])

<<<<<<< HEAD
        else:
            callback_metric = PrintPerformanceMetricEachNUpdates(model, dataset,
                                                           gt_id=params['OUTPUTS_IDS_DATASET'][0],
                                                           metric_name=params['METRICS'],
                                                           set_name=params['EVAL_ON_SETS'],
                                                           batch_size=params['BATCH_SIZE'],
                                                           each_n_updates=params['EVAL_EACH'],
                                                           extra_vars=extra_vars,
                                                           reload_epoch=params['RELOAD'],
                                                           is_text=True, index2word_y=vocab, # text info
                                                           sampling_type=params['SAMPLING'], # text info
                                                           beam_search=params['BEAM_SEARCH'],
                                                           save_path=model.model_path,
                                                           start_eval_on_epoch=params['START_EVAL_ON_EPOCH'],
                                                           write_samples=True,
                                                           write_type=params['SAMPLING_SAVE_MODE'],
                                                           early_stop=params['EARLY_STOP'],
                                                           patience=params['PATIENCE'],
                                                           stop_metric=params['STOP_METRIC'],
                                                           verbose=params['VERBOSE'])
        callbacks.append(callback_metric)

        if params['SAMPLE_ON_SETS']:
            # Evaluate sampling
            extra_vars = {'language': params['TRG_LAN'], 'n_parallel_loaders': params['PARALLEL_LOADERS']}
            vocab = dataset.vocabulary[params['OUTPUTS_IDS_DATASET'][0]]['idx2words']
            for s in params['EVAL_ON_SETS']:
                extra_vars[s] = dict()
                extra_vars[s]['references'] = dataset.extra_variables[s][params['OUTPUTS_IDS_DATASET'][0]]
                extra_vars[s]['tokenize_f'] = eval('dataset.' + params['TOKENIZATION_METHOD'])
            if params['BEAM_SIZE']:
                extra_vars['beam_size'] = params['BEAM_SIZE']
                extra_vars['maxlen'] = params['MAX_OUTPUT_TEXT_LEN']
                extra_vars['model_inputs'] = params['INPUTS_IDS_MODEL']
                extra_vars['model_outputs'] = params['OUTPUTS_IDS_MODEL']
                extra_vars['dataset_inputs'] = params['INPUTS_IDS_DATASET']
                extra_vars['dataset_outputs'] = params['OUTPUTS_IDS_DATASET']
                extra_vars['normalize'] =  params['NORMALIZE_SAMPLING']
                extra_vars['alpha_factor'] =  params['ALPHA_FACTOR']

            callback_sampling = SampleEachNUpdates(model, dataset, gt_id=params['OUTPUTS_IDS_DATASET'][0],
                                                                   set_name=params['SAMPLE_ON_SETS'],
                                                                   n_samples=params['N_SAMPLES'],
                                                                   each_n_updates=params['SAMPLE_EACH_UPDATES'],
                                                                   extra_vars=extra_vars,
                                                                   reload_epoch=params['RELOAD'],
                                                                   is_text=True, index2word_y=vocab,  # text info
                                                                   sampling_type=params['SAMPLING'],  # text info
                                                                   beam_search=params['BEAM_SEARCH'],
                                                                   start_sampling_on_epoch=params['START_SAMPLING_ON_EPOCH'],
                                                                   verbose=params['VERBOSE'])
            callbacks.append(callback_sampling)
=======
        callbacks.append(callback_metric)

    if params['SAMPLE_ON_SETS']:
        # Write some samples
        extra_vars = {'language': params.get('TRG_LAN', 'en'), 'n_parallel_loaders': params['PARALLEL_LOADERS']}
        vocab = dataset.vocabulary[params['OUTPUTS_IDS_DATASET'][0]]['idx2words']
        for s in params['EVAL_ON_SETS']:
            extra_vars[s] = dict()
            extra_vars[s]['references'] = dataset.extra_variables[s][params['OUTPUTS_IDS_DATASET'][0]]
            extra_vars[s]['tokenize_f'] = eval('dataset.' + params['TOKENIZATION_METHOD'])
        if params['BEAM_SIZE']:
            extra_vars['beam_size'] = params['BEAM_SIZE']
            extra_vars['maxlen'] = params['MAX_OUTPUT_TEXT_LEN_TEST']
            extra_vars['optimized_search'] = params['OPTIMIZED_SEARCH']
            extra_vars['model_inputs'] = params['INPUTS_IDS_MODEL']
            extra_vars['model_outputs'] = params['OUTPUTS_IDS_MODEL']
            extra_vars['dataset_inputs'] = params['INPUTS_IDS_DATASET']
            extra_vars['dataset_outputs'] = params['OUTPUTS_IDS_DATASET']
            extra_vars['normalize'] = params['NORMALIZE_SAMPLING']
            extra_vars['alpha_factor'] = params['ALPHA_FACTOR']

        callback_sampling = utils.callbacks.SampleEachNUpdates(model,
                                                               dataset,
                                                               gt_id=params['OUTPUTS_IDS_DATASET'][0],
                                                               set_name=params['SAMPLE_ON_SETS'],
                                                               n_samples=params['N_SAMPLES'],
                                                               each_n_updates=params['SAMPLE_EACH_UPDATES'],
                                                               extra_vars=extra_vars,
                                                               reload_epoch=params['RELOAD'],
                                                               batch_size=params['BATCH_SIZE'],
                                                               is_text=True,
                                                               index2word_y=vocab,  # text info
                                                               in_pred_idx=params['INPUTS_IDS_DATASET'][0],
                                                               sampling_type=params['SAMPLING'],  # text info
                                                               beam_search=params['BEAM_SEARCH'],
                                                               start_sampling_on_epoch=params['START_SAMPLING'
                                                                                              '_ON_EPOCH'],
                                                               verbose=params['VERBOSE'])
        callbacks.append(callback_sampling)
>>>>>>> 2c7f6513f5b7f657afb8ebf68d177d22c0e85b39
    return callbacks



def check_params(params):
    if 'Glove' in params['MODEL_TYPE'] and params['GLOVE_VECTORS'] is None:
        logger.warning("You set a model that uses pretrained word vectors but you didn't specify a vector file."
                       "We'll train WITHOUT pretrained embeddings!")
    if params["USE_DROPOUT"] and params["USE_BATCH_NORMALIZATION"]:
        logger.warning("It's not recommended to use both dropout and batch normalization")


if __name__ == "__main__":

    parameters = load_parameters()
    try:
        for arg in sys.argv[1:]:
            k, v = arg.split('=')
            parameters[k] = ast.literal_eval(v)
    except ValueError:
        print 'Overwritten arguments must have the form key=Value'
        exit(1)
    check_params(parameters)
    if parameters['MODE'] == 'training':
        logging.info('Running training.')
        train_model(parameters)
    elif parameters['MODE'] == 'sampling':
        logging.info('Running sampling.')
        apply_Video_model(parameters)

    logging.info('Done!')
