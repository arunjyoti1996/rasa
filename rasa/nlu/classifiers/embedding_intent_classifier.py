import logging
import numpy as np
import os
import pickle
import scipy.sparse
import warnings
import tensorflow as tf
import tensorflow_addons as tfa

from typing import Any, Dict, List, Optional, Text, Tuple, Union, Callable

from rasa.nlu.extractors import EntityExtractor
from rasa.nlu.test import determine_token_labels
from rasa.nlu.tokenizers.tokenizer import Token
from rasa.nlu.classifiers import LABEL_RANKING_LENGTH
from rasa.nlu.components import any_of
from rasa.utils import train_utils
from rasa.utils.tensorflow import tf_layers, tf_models
from rasa.utils.tensorflow.tf_model_data import RasaModelData, FeatureSignature
from rasa.nlu.constants import (
    INTENT_ATTRIBUTE,
    TEXT_ATTRIBUTE,
    ENTITIES_ATTRIBUTE,
    SPARSE_FEATURE_NAMES,
    DENSE_FEATURE_NAMES,
    TOKENS_NAMES,
)
from rasa.nlu.config import RasaNLUModelConfig
from rasa.nlu.training_data import TrainingData
from rasa.nlu.model import Metadata
from rasa.nlu.training_data import Message


logger = logging.getLogger(__name__)


# constants - configuration parameters
HIDDEN_LAYERS_SIZES_TEXT = "hidden_layers_sizes_text"
HIDDEN_LAYERS_SIZES_LABEL = "hidden_layers_sizes_label"
SHARE_HIDDEN_LAYERS = "share_hidden_layers"
TRANSFORMER_SIZE = "transformer_size"
NUM_TRANSFORMER_LAYERS = "number_of_transformer_layers"
NUM_HEADS = "number_of_attention_heads"
POS_ENCODING = "positional_encoding"
MAX_SEQ_LENGTH = "maximum_sequence_length"
BATCH_SIZES = "batch_sizes"
BATCH_STRATEGY = "batch_strategy"
EPOCHS = "epochs"
RANDOM_SEED = "random_seed"
LEARNING_RATE = "learning_rate"
DENSE_DIM = "dense_dimensions"
EMBED_DIM = "embedding_dimension"
NUM_NEG = "number_of_negative_examples"
SIMILARITY_TYPE = "similarity_type"
LOSS_TYPE = "loss_type"
MU_POS = "maximum_positive_similarity"
MU_NEG = "maximum_negative_similarity"
USE_MAX_SIM_NEG = "use_maximum_negative_similarity"
SCALE_LOSS = "scale_loss"
C2 = "l2_regularization"
C_EMB = "c_emb"
DROPRATE = "droprate"
UNIDIRECTIONAL_ENCODER = "unidirectional_encoder"
EVAL_NUM_EPOCHS = "evaluate_every_number_of_epochs"
EVAL_NUM_EXAMPLES = "evaluate_on_number_of_examples"
INTENT_CLASSIFICATION = "perform_intent_classification"
ENTITY_RECOGNITION = "perform_entity_recognition"
MASKED_LM = "use_masked_language_model"
SPARSE_INPUT_DROPOUT = "use_sparse_input_dropout"


class EmbeddingIntentClassifier(EntityExtractor):
    """label classifier using supervised embeddings.

    The embedding intent classifier embeds user inputs
    and intent labels into the same space.
    Supervised embeddings are trained by maximizing similarity between them.
    It also provides rankings of the labels that did not "win".

    The embedding intent classifier needs to be preceded by
    a featurizer in the pipeline.
    This featurizer creates the features used for the embeddings.
    It is recommended to use ``CountVectorsFeaturizer`` that
    can be optionally preceded by ``SpacyNLP`` and ``SpacyTokenizer``.

    Based on the starspace idea from: https://arxiv.org/abs/1709.03856.
    However, in this implementation the `mu` parameter is treated differently
    and additional hidden layers are added together with dropout.
    """

    provides = ["intent", "intent_ranking", "entities"]

    requires = [
        any_of(
            DENSE_FEATURE_NAMES[TEXT_ATTRIBUTE], SPARSE_FEATURE_NAMES[TEXT_ATTRIBUTE]
        )
    ]

    # default properties (DOC MARKER - don't remove)
    defaults = {
        # nn architecture
        # sizes of hidden layers before the embedding layer for input words
        # the number of hidden layers is thus equal to the length of this list
        HIDDEN_LAYERS_SIZES_TEXT: [],
        # sizes of hidden layers before the embedding layer for intent labels
        # the number of hidden layers is thus equal to the length of this list
        HIDDEN_LAYERS_SIZES_LABEL: [],
        # Whether to share the hidden layer weights between input words and labels
        SHARE_HIDDEN_LAYERS: False,
        # number of units in transformer
        TRANSFORMER_SIZE: 256,
        # number of transformer layers
        NUM_TRANSFORMER_LAYERS: 2,
        # number of attention heads in transformer
        NUM_HEADS: 4,
        # type of positional encoding in transformer
        POS_ENCODING: "timing",  # string 'timing' or 'emb'
        # max sequence length if pos_encoding='emb'
        MAX_SEQ_LENGTH: 256,
        # training parameters
        # initial and final batch sizes - batch size will be
        # linearly increased for each epoch
        BATCH_SIZES: [64, 256],
        # how to create batches
        BATCH_STRATEGY: "balanced",  # string 'sequence' or 'balanced'
        # number of epochs
        EPOCHS: 300,
        # set random seed to any int to get reproducible results
        RANDOM_SEED: None,
        # optimizer
        LEARNING_RATE: 0.001,
        # embedding parameters
        # default dense dimension used if no dense features are present
        DENSE_DIM: {"text": 512, "label": 20},
        # dimension size of embedding vectors
        EMBED_DIM: 20,
        # the type of the similarity
        NUM_NEG: 20,
        # flag if minimize only maximum similarity over incorrect actions
        SIMILARITY_TYPE: "auto",  # string 'auto' or 'cosine' or 'inner'
        # the type of the loss function
        LOSS_TYPE: "softmax",  # string 'softmax' or 'margin'
        # how similar the algorithm should try
        # to make embedding vectors for correct labels
        MU_POS: 0.8,  # should be 0.0 < ... < 1.0 for 'cosine'
        # maximum negative similarity for incorrect labels
        MU_NEG: -0.4,  # should be -1.0 < ... < 1.0 for 'cosine'
        # flag: if true, only minimize the maximum similarity for incorrect labels
        USE_MAX_SIM_NEG: True,
        # scale loss inverse proportionally to confidence of correct prediction
        SCALE_LOSS: True,
        # regularization parameters
        # the scale of L2 regularization
        C2: 0.002,
        # the scale of how critical the algorithm should be of minimizing the
        # maximum similarity between embeddings of different labels
        C_EMB: 0.8,
        # dropout rate for rnn
        DROPRATE: 0.2,
        # use a unidirectional or bidirectional encoder
        UNIDIRECTIONAL_ENCODER: True,
        # visualization of accuracy
        # how often to calculate training accuracy
        EVAL_NUM_EPOCHS: 20,  # small values may hurt performance
        # how many examples to use for calculation of training accuracy
        EVAL_NUM_EXAMPLES: 0,  # large values may hurt performance
        # model config
        # if true intent classification is trained and intent predicted
        INTENT_CLASSIFICATION: True,
        # if true named entity recognition is trained and entities predicted
        ENTITY_RECOGNITION: True,
        MASKED_LM: False,
        SPARSE_INPUT_DROPOUT: False,
    }
    # end default properties (DOC MARKER - don't remove)

    # init helpers
    def _check_config_parameters(self) -> None:
        if (
            self.component_config[SHARE_HIDDEN_LAYERS]
            and self.component_config[HIDDEN_LAYERS_SIZES_TEXT]
            != self.component_config[HIDDEN_LAYERS_SIZES_LABEL]
        ):
            raise ValueError(
                "If hidden layer weights are shared,"
                "hidden_layer_sizes for text and label must coincide."
            )

        if self.component_config[SIMILARITY_TYPE] == "auto":
            if self.component_config[LOSS_TYPE] == "softmax":
                self.component_config[SIMILARITY_TYPE] = "inner"
            elif self.component_config[LOSS_TYPE] == "margin":
                self.component_config[SIMILARITY_TYPE] = "cosine"

        if self.component_config[EVAL_NUM_EPOCHS] < 1:
            self.component_config[EVAL_NUM_EPOCHS] = self.component_config[EPOCHS]

    # package safety checks
    @classmethod
    def required_packages(cls) -> List[Text]:
        return ["tensorflow"]

    def __init__(
        self,
        component_config: Optional[Dict[Text, Any]] = None,
        inverted_label_dict: Optional[Dict[int, Text]] = None,
        inverted_tag_dict: Optional[Dict[int, Text]] = None,
        model: Optional[tf_models.RasaModel] = None,
        predict_func: Optional[Callable] = None,
        batch_tuple_sizes: Optional[Dict] = None,
        attention_weights: Optional[tf.Tensor] = None,
    ) -> None:
        """Declare instance variables with default values"""

        super().__init__(component_config)

        self._check_config_parameters()

        # transform numbers to labels
        self.inverted_label_dict = inverted_label_dict
        self.inverted_tag_dict = inverted_tag_dict

        self.model = model
        self.predict_func = predict_func

        # encode all label_ids with numbers
        self._label_data = None

        # keep the input tuple sizes in self.batch_in
        self.batch_tuple_sizes = batch_tuple_sizes

        # internal tf instances
        self._iterator = None
        self._train_op = None
        self._is_training = None

        # number of entity tags
        self.num_tags = 0

        self.attention_weights = attention_weights

        self._tf_config = train_utils.load_tf_config(self.component_config)

        self.data_example = None

    # training data helpers:
    @staticmethod
    def _create_label_id_dict(
        training_data: TrainingData, attribute: Text
    ) -> Dict[Text, int]:
        """Create label_id dictionary"""

        distinct_label_ids = {
            example.get(attribute) for example in training_data.intent_examples
        } - {None}
        return {
            label_id: idx for idx, label_id in enumerate(sorted(distinct_label_ids))
        }

    @staticmethod
    def _create_tag_id_dict(training_data: TrainingData) -> Dict[Text, int]:
        """Create label_id dictionary"""

        distinct_tag_ids = set(
            [
                e["entity"]
                for example in training_data.entity_examples
                for e in example.get(ENTITIES_ATTRIBUTE)
            ]
        ) - {None}

        tag_id_dict = {
            tag_id: idx for idx, tag_id in enumerate(sorted(distinct_tag_ids), 1)
        }
        tag_id_dict["O"] = 0

        return tag_id_dict

    @staticmethod
    def _find_example_for_label(
        label: Text, examples: List[Message], attribute: Text
    ) -> Optional[Message]:
        for ex in examples:
            if ex.get(attribute) == label:
                return ex
        return None

    @staticmethod
    def _find_example_for_tag(
        tag: Text, examples: List[Message], attribute: Text
    ) -> Optional[Message]:
        for ex in examples:
            for e in ex.get(attribute):
                if e["entity"] == tag:
                    return ex
        return None

    @staticmethod
    def _check_labels_features_exist(
        labels_example: List[Message], attribute: Text
    ) -> bool:
        """Check if all labels have features set"""

        for label_example in labels_example:
            if (
                label_example.get(SPARSE_FEATURE_NAMES[attribute]) is None
                and label_example.get(DENSE_FEATURE_NAMES[attribute]) is None
            ):
                return False
        return True

    @staticmethod
    def _extract_and_add_features(
        message: Message, attribute: Text
    ) -> Tuple[Optional[scipy.sparse.spmatrix], Optional[np.ndarray]]:
        sparse_features = None
        dense_features = None

        if message.get(SPARSE_FEATURE_NAMES[attribute]) is not None:
            sparse_features = message.get(SPARSE_FEATURE_NAMES[attribute])

        if message.get(DENSE_FEATURE_NAMES[attribute]) is not None:
            dense_features = message.get(DENSE_FEATURE_NAMES[attribute])

        if sparse_features is not None and dense_features is not None:
            if sparse_features.shape[0] != dense_features.shape[0]:
                raise ValueError(
                    f"Sequence dimensions for sparse and dense features "
                    f"don't coincide in '{message.text}' for attribute '{attribute}'."
                )

        return sparse_features, dense_features

    def check_input_dimension_consistency(self, model_data: RasaModelData):
        if self.component_config[SHARE_HIDDEN_LAYERS]:
            num_text_features = model_data.get_feature_dimension("text_features")
            num_intent_features = model_data.get_feature_dimension("label_features")

            if num_text_features != num_intent_features:
                raise ValueError(
                    "If embeddings are shared "
                    "text features and label features "
                    "must coincide. Check the output dimensions of previous components."
                )

    def _extract_labels_precomputed_features(
        self, label_examples: List[Message], attribute: Text = INTENT_ATTRIBUTE
    ) -> List[np.ndarray]:
        """Collect precomputed encodings"""

        sparse_features = []
        dense_features = []

        for e in label_examples:
            _sparse, _dense = self._extract_and_add_features(e, attribute)
            if _sparse is not None:
                sparse_features.append(_sparse)
            if _dense is not None:
                dense_features.append(_dense)

        sparse_features = np.array(sparse_features)
        dense_features = np.array(dense_features)

        return [sparse_features, dense_features]

    @staticmethod
    def _compute_default_label_features(
        labels_example: List[Message],
    ) -> List[np.ndarray]:
        """Compute one-hot representation for the labels"""

        return [
            np.array(
                [
                    np.expand_dims(a, 0)
                    for a in np.eye(len(labels_example), dtype=np.float32)
                ]
            )
        ]

    def _create_label_data(
        self,
        training_data: TrainingData,
        label_id_dict: Dict[Text, int],
        attribute: Text,
    ) -> RasaModelData:
        """Create matrix with label_ids encoded in rows as bag of words.

        Find a training example for each label and get the encoded features
        from the corresponding Message object.
        If the features are already computed, fetch them from the message object
        else compute a one hot encoding for the label as the feature vector.
        """

        # Collect one example for each label
        labels_idx_example = []
        for label_name, idx in label_id_dict.items():
            label_example = self._find_example_for_label(
                label_name, training_data.intent_examples, attribute
            )
            labels_idx_example.append((idx, label_example))

        # Sort the list of tuples based on label_idx
        labels_idx_example = sorted(labels_idx_example, key=lambda x: x[0])
        labels_example = [example for (_, example) in labels_idx_example]

        # Collect features, precomputed if they exist, else compute on the fly
        if self._check_labels_features_exist(labels_example, attribute):
            features = self._extract_labels_precomputed_features(
                labels_example, attribute
            )
        else:
            features = self._compute_default_label_features(labels_example)

        label_data = RasaModelData()
        label_data.add_features("label_features", features)
        label_data.add_mask("label_mask", "label_features")

        return label_data

    def _use_default_label_features(self, label_ids: np.ndarray) -> List[np.ndarray]:
        return [
            np.array(
                [
                    self._label_data.get("label_features")[0][label_id]
                    for label_id in label_ids
                ]
            )
        ]

    def _create_model_data(
        self,
        training_data: List[Message],
        label_id_dict: Optional[Dict[Text, int]] = None,
        tag_id_dict: Optional[Dict[Text, int]] = None,
        label_attribute: Optional[Text] = None,
    ) -> RasaModelData:
        """Prepare data for training and create a SessionDataType object"""

        X_sparse = []
        X_dense = []
        Y_sparse = []
        Y_dense = []
        label_ids = []
        tag_ids = []

        for e in training_data:
            _sparse, _dense = self._extract_and_add_features(e, TEXT_ATTRIBUTE)
            if _sparse is not None:
                X_sparse.append(_sparse)
            if _dense is not None:
                X_dense.append(_dense)

            if e.get(label_attribute):
                _sparse, _dense = self._extract_and_add_features(e, label_attribute)
                if _sparse is not None:
                    Y_sparse.append(_sparse)
                if _dense is not None:
                    Y_dense.append(_dense)

                if label_id_dict:
                    label_ids.append(label_id_dict[e.get(label_attribute)])

            if self.component_config[ENTITY_RECOGNITION] and tag_id_dict:
                _tags = []
                for t in e.get(TOKENS_NAMES[TEXT_ATTRIBUTE]):
                    _tag = determine_token_labels(t, e.get(ENTITIES_ATTRIBUTE), None)
                    _tags.append(tag_id_dict[_tag])
                # transpose to have seq_len x 1
                tag_ids.append(np.array([_tags]).T)

        X_sparse = np.array(X_sparse)
        X_dense = np.array(X_dense)
        Y_sparse = np.array(Y_sparse)
        Y_dense = np.array(Y_dense)
        label_ids = np.array(label_ids)
        tag_ids = np.array(tag_ids)

        model_data = RasaModelData(label_key="label_ids")
        model_data.add_features("text_features", [X_sparse, X_dense])
        model_data.add_features("label_features", [Y_sparse, Y_dense])
        if label_attribute and model_data.feature_not_exists("label_features"):
            # no label features are present, get default features from _label_data
            model_data.add_features(
                "label_features", self._use_default_label_features(label_ids)
            )

        # explicitly add last dimension to label_ids
        # to track correctly dynamic sequences
        model_data.add_features("label_ids", [np.expand_dims(label_ids, -1)])
        model_data.add_features("tag_ids", [tag_ids])

        model_data.add_mask("text_mask", "text_features")
        model_data.add_mask("label_mask", "label_features")

        return model_data

    # train helpers
    def preprocess_train_data(self, training_data: TrainingData) -> RasaModelData:
        """Prepares data for training.

        Performs sanity checks on training data, extracts encodings for labels.
        """
        label_id_dict = self._create_label_id_dict(
            training_data, attribute=INTENT_ATTRIBUTE
        )
        self.inverted_label_dict = {v: k for k, v in label_id_dict.items()}

        self._label_data = self._create_label_data(
            training_data, label_id_dict, attribute=INTENT_ATTRIBUTE
        )

        tag_id_dict = self._create_tag_id_dict(training_data)
        self.inverted_tag_dict = {v: k for k, v in tag_id_dict.items()}

        model_data = self._create_model_data(
            training_data.training_examples,
            label_id_dict,
            tag_id_dict,
            label_attribute=INTENT_ATTRIBUTE,
        )

        self.num_tags = len(self.inverted_tag_dict)

        self.check_input_dimension_consistency(model_data)

        return model_data

    @staticmethod
    def _check_enough_labels(model_data: RasaModelData) -> bool:
        return len(np.unique(model_data.get("label_ids"))) >= 2

    def train(
        self,
        training_data: TrainingData,
        cfg: Optional[RasaNLUModelConfig] = None,
        **kwargs: Any,
    ) -> None:
        """Train the embedding intent classifier on a data set."""

        logger.debug("Started training embedding classifier.")

        # set numpy random seed
        np.random.seed(self.component_config[RANDOM_SEED])

        model_data = self.preprocess_train_data(training_data)

        if self.component_config[INTENT_CLASSIFICATION]:
            possible_to_train = self._check_enough_labels(model_data)

            if not possible_to_train:
                logger.error(
                    "Can not train intent classifier. "
                    "Need at least 2 different classes. "
                    "Skipping training of classifier."
                )
                return

        # keep one example for persisting and loading
        self.data_example = {k: [v[:1] for v in vs] for k, vs in model_data.items()}

        # TODO set it in the model
        # set random seed
        tf.random.set_seed(self.component_config[RANDOM_SEED])

        model_data_signature = model_data.get_signature()

        self.model = DIET(
            model_data_signature,
            self._label_data,
            self.inverted_tag_dict,
            self.component_config,
        )

        self.model.fit(
            model_data,
            self.component_config[EPOCHS],
            self.component_config[BATCH_SIZES],
            self.component_config[EVAL_NUM_EXAMPLES],
            self.component_config[EVAL_NUM_EPOCHS],
            batch_strategy=self.component_config[BATCH_STRATEGY],
            random_seed=self.component_config[RANDOM_SEED],
        )

    # process helpers
    def _predict(self, message: Message) -> Optional[Dict[Text, tf.Tensor]]:
        if self.model is None or self.predict_func is None:
            return

        # create session data from message and convert it into a batch of 1
        model_data = self._create_model_data([message])
        predict_dataset = model_data.as_tf_dataset(1)
        batch_in = next(iter(predict_dataset))

        return self.predict_func(batch_in)

    def _predict_label(
        self, out: Dict[Text, tf.Tensor]
    ) -> Tuple[Dict[Text, Any], List[Dict[Text, Any]]]:
        """Predicts the intent of the provided message."""

        label = {"name": None, "confidence": 0.0}
        label_ranking = []

        if self.model is None:
            logger.error(
                "There is no trained tf.session: "
                "component is either not trained or "
                "didn't receive enough training data."
            )
            return label, label_ranking

        message_sim = out["i_scores"].numpy()

        message_sim = message_sim.flatten()  # sim is a matrix

        label_ids = message_sim.argsort()[::-1]
        message_sim[::-1].sort()
        message_sim = message_sim.tolist()

        # if X contains all zeros do not predict some label
        if label_ids.size > 0:
            label = {
                "name": self.inverted_label_dict[label_ids[0]],
                "confidence": message_sim[0],
            }

            ranking = list(zip(list(label_ids), message_sim))
            ranking = ranking[:LABEL_RANKING_LENGTH]
            label_ranking = [
                {"name": self.inverted_label_dict[label_idx], "confidence": score}
                for label_idx, score in ranking
            ]

        return label, label_ranking

    def _predict_entities(
        self, out: Dict[Text, tf.Tensor], message: Message
    ) -> List[Dict]:
        if self.model is None:
            logger.error(
                "There is no trained tf.session: "
                "component is either not trained or "
                "didn't receive enough training data"
            )
            return []

        # load tf graph and session
        predictions = out["e_ids"].numpy()

        tags = [self.inverted_tag_dict[p] for p in predictions[0]]

        entities = self._convert_tags_to_entities(
            message.text, message.get("tokens", []), tags
        )

        extracted = self.add_extractor_name(entities)
        entities = message.get("entities", []) + extracted

        return entities

    @staticmethod
    def _convert_tags_to_entities(
        text: Text, tokens: List[Token], tags: List[Text]
    ) -> List[Dict[Text, Any]]:
        entities = []
        last_tag = "O"
        for token, tag in zip(tokens, tags):
            if tag == "O":
                last_tag = tag
                continue

            # new tag found
            if last_tag != tag:
                entity = {
                    "entity": tag,
                    "start": token.start,
                    "end": token.end,
                    "extractor": "DIET",
                }
                entities.append(entity)

            # belongs to last entity
            elif last_tag == tag:
                entities[-1]["end"] = token.end

            last_tag = tag

        for entity in entities:
            entity["value"] = text[entity["start"] : entity["end"]]

        return entities

    def process(self, message: Message, **kwargs: Any) -> None:
        """Return the most likely label and its similarity to the input."""

        out = self._predict(message)

        if self.component_config[INTENT_CLASSIFICATION]:
            label, label_ranking = self._predict_label(out)

            message.set("intent", label, add_to_output=True)
            message.set("intent_ranking", label_ranking, add_to_output=True)

        if self.component_config[ENTITY_RECOGNITION]:
            entities = self._predict_entities(out, message)

            message.set("entities", entities, add_to_output=True)

    def persist(self, file_name: Text, model_dir: Text) -> Dict[Text, Any]:
        """Persist this model into the passed directory.

        Return the metadata necessary to load the model again.
        """

        if self.model is None:
            return {"file": None}

        tf_model_file = os.path.join(model_dir, file_name + ".tf_model")

        try:
            os.makedirs(os.path.dirname(tf_model_file))
        except OSError as e:
            # be happy if someone already created the path
            import errno

            if e.errno != errno.EEXIST:
                raise

        self.model.save_weights(tf_model_file, save_format="tf")

        with open(os.path.join(model_dir, file_name + ".data_example.pkl"), "wb") as f:
            pickle.dump(self.data_example, f)

        with open(os.path.join(model_dir, file_name + ".label_data.pkl"), "wb") as f:
            pickle.dump(self._label_data, f)

        with open(
            os.path.join(model_dir, file_name + ".inv_label_dict.pkl"), "wb"
        ) as f:
            pickle.dump(self.inverted_label_dict, f)

        with open(os.path.join(model_dir, file_name + ".inv_tag_dict.pkl"), "wb") as f:
            pickle.dump(self.inverted_tag_dict, f)

        with open(os.path.join(model_dir, file_name + ".tf_config.pkl"), "wb") as f:
            pickle.dump(self._tf_config, f)

        with open(
            os.path.join(model_dir, file_name + ".batch_tuple_sizes.pkl"), "wb"
        ) as f:
            pickle.dump(self.batch_tuple_sizes, f)

        return {"file": file_name}

    @classmethod
    def load(
        cls,
        meta: Dict[Text, Any],
        model_dir: Text = None,
        model_metadata: "Metadata" = None,
        cached_component: Optional["EmbeddingIntentClassifier"] = None,
        **kwargs: Any,
    ) -> "EmbeddingIntentClassifier":
        """Loads the trained model from the provided directory."""

        if not model_dir or not meta.get("file"):
            warnings.warn(
                f"Failed to load nlu model. "
                f"Maybe path '{os.path.abspath(model_dir)}' doesn't exist."
            )
            return cls(component_config=meta)

        file_name = meta.get("file")
        tf_model_file = os.path.join(model_dir, file_name + ".tf_model")

        # with open(os.path.join(model_dir, file_name + ".tf_config.pkl"), "rb") as f:
        #    _tf_config = pickle.load(f)

        with open(os.path.join(model_dir, file_name + ".data_example.pkl"), "rb") as f:
            model_data_example = RasaModelData(
                label_key="label_ids", data=pickle.load(f)
            )

        with open(os.path.join(model_dir, file_name + ".label_data.pkl"), "rb") as f:
            label_data = pickle.load(f)

        with open(
            os.path.join(model_dir, file_name + ".inv_label_dict.pkl"), "rb"
        ) as f:
            inv_label_dict = pickle.load(f)

        with open(os.path.join(model_dir, file_name + ".inv_tag_dict.pkl"), "rb") as f:
            inv_tag_dict = pickle.load(f)

        with open(
            os.path.join(model_dir, file_name + ".batch_tuple_sizes.pkl"), "rb"
        ) as f:
            batch_tuple_sizes = pickle.load(f)

        if meta[SIMILARITY_TYPE] == "auto":
            if meta[LOSS_TYPE] == "softmax":
                meta[SIMILARITY_TYPE] = "inner"
            elif meta[LOSS_TYPE] == "margin":
                meta[SIMILARITY_TYPE] = "cosine"

        model = DIET(model_data_example.get_signature(), label_data, inv_tag_dict, meta)

        logger.debug("Loading the model ...")
        model.fit(
            model_data_example,
            1,
            1,
            0,
            0,
            batch_strategy=meta[BATCH_STRATEGY],
            silent=True,  # don't confuse users with training output
            eager=True,  # no need to build tf graph, eager is faster here
        )
        model.load_weights(tf_model_file)

        # build the graph for prediction
        model.set_training_phase(False)
        model_data = RasaModelData(
            label_key="label_ids",
            data={k: vs for k, vs in model_data_example.items() if "text" in k},
        )
        model.data_signature = model_data.get_signature()
        model.build_for_predict(model_data)
        predict_dataset = model_data.as_tf_dataset(
            1, batch_strategy="sequence", shuffle=False
        )
        predict_func = tf.function(
            func=model.predict, input_signature=[predict_dataset.element_spec]
        )
        batch_in = next(iter(predict_dataset))
        predict_func(batch_in)
        logger.debug("Finished loading the model.")

        return cls(
            component_config=meta,
            inverted_label_dict=inv_label_dict,
            inverted_tag_dict=inv_tag_dict,
            model=model,
            predict_func=predict_func,
            batch_tuple_sizes=batch_tuple_sizes,
        )


class DIET(tf_models.RasaModel):
    def __init__(
        self,
        data_signature: Dict[Text, List[FeatureSignature]],
        label_data: RasaModelData,
        inverted_tag_dict: Dict[int, Text],
        config: Dict[Text, Any],
    ) -> None:
        super().__init__(name="DIET")

        # data
        self.data_signature = data_signature
        label_batch = label_data.prepare_batch()
        self.tf_label_data = self.batch_to_model_data_format(
            label_batch, label_data.get_signature()
        )
        self._num_tags = len(inverted_tag_dict)

        self.config = config

        # tf objects
        self._prepare_layers()

        # tf tensors
        self.training = tf.ones((), tf.bool)

        # tf training
        self._optimizer = tf.keras.optimizers.Adam(config[LEARNING_RATE])
        self.intent_acc = tf.keras.metrics.Mean(name="i_acc")
        self.intent_loss = tf.keras.metrics.Mean(name="i_loss")
        self.mask_loss = tf.keras.metrics.Mean(name="m_loss")
        self.mask_acc = tf.keras.metrics.Mean(name="m_acc")
        self.entity_loss = tf.keras.metrics.Mean(name="e_loss")
        self.entity_f1 = tf.keras.metrics.Mean(name="e_f1")

        # persist
        self.all_labels_embed = None
        self.batch_tuple_sizes = None

    def _prepare_layers(self) -> None:
        self._tf_layers = {}
        self._prepare_sequence_layers()
        self._tf_layers["embed"] = {}
        self._prepare_mask_lm_layers()
        self._prepare_intent_classification_layers()
        self._prepare_entity_recognition_layers()

    def _prepare_sequence_layers(self):
        self._tf_layers["sparse_dropout"] = tf_layers.SparseDropout(
            rate=self.config[DROPRATE]
        )
        self._tf_layers["sparse_to_dense"] = {
            "text": self._create_sparse_dense_layer(
                self.data_signature["text_features"],
                "text",
                self.config[C2],
                self.config[DENSE_DIM]["text"],
            ),
            "label": self._create_sparse_dense_layer(
                self.data_signature["label_features"],
                "label",
                self.config[C2],
                self.config[DENSE_DIM]["label"],
            ),
        }
        self._tf_layers["ffnn"] = {
            "text": tf_layers.ReluFfn(
                self.config[HIDDEN_LAYERS_SIZES_TEXT],
                self.config[DROPRATE],
                self.config[C2],
                "text_intent" if self.config[SHARE_HIDDEN_LAYERS] else "text",
            ),
            "label": tf_layers.ReluFfn(
                self.config[HIDDEN_LAYERS_SIZES_LABEL],
                self.config[DROPRATE],
                self.config[C2],
                "text_intent" if self.config[SHARE_HIDDEN_LAYERS] else "label",
            ),
        }
        self._tf_layers["transformer"] = (
            tf_layers.TransformerEncoder(
                self.config[NUM_TRANSFORMER_LAYERS],
                self.config[TRANSFORMER_SIZE],
                self.config[NUM_HEADS],
                self.config[TRANSFORMER_SIZE] * 4,
                self.config[MAX_SEQ_LENGTH],
                self.config[C2],
                self.config[DROPRATE],
                self.config[UNIDIRECTIONAL_ENCODER],
                name="text_encoder",
            )
            if self.config[NUM_TRANSFORMER_LAYERS] > 0
            else lambda x, mask, training: x
        )

    def _prepare_mask_lm_layers(self):
        self._tf_layers["input_mask"] = tf_layers.InputMask()
        self._tf_layers["embed"]["text_mask"] = tf_layers.Embed(
            self.config[EMBED_DIM],
            self.config[C2],
            "text_mask",
            self.config[SIMILARITY_TYPE],
        )
        self._tf_layers["embed"]["text_token"] = tf_layers.Embed(
            self.config[EMBED_DIM],
            self.config[C2],
            "text_token",
            self.config[SIMILARITY_TYPE],
        )
        self._tf_layers["loss_mask"] = tf_layers.DotProductLoss(
            self.config[NUM_NEG],
            self.config[LOSS_TYPE],
            self.config[MU_POS],
            self.config[MU_NEG],
            self.config[USE_MAX_SIM_NEG],
            self.config[C_EMB],
            self.config[SCALE_LOSS],
        )

    def _prepare_intent_classification_layers(self):
        self._tf_layers["embed"]["text"] = tf_layers.Embed(
            self.config[EMBED_DIM],
            self.config[C2],
            "text",
            self.config[SIMILARITY_TYPE],
        )
        self._tf_layers["embed"]["label"] = tf_layers.Embed(
            self.config[EMBED_DIM],
            self.config[C2],
            "label",
            self.config[SIMILARITY_TYPE],
        )
        self._tf_layers["loss_label"] = tf_layers.DotProductLoss(
            self.config[NUM_NEG],
            self.config[LOSS_TYPE],
            self.config[MU_POS],
            self.config[MU_NEG],
            self.config[USE_MAX_SIM_NEG],
            self.config[C_EMB],
            self.config[SCALE_LOSS],
        )

    def _prepare_entity_recognition_layers(self):
        self._tf_layers["embed"]["logits"] = tf_layers.Embed(
            self._num_tags, self.config[C2], "logits"
        )
        self._tf_layers["crf"] = tf_layers.CRF(self._num_tags, self.config[C2])
        self._tf_layers["crf_f1_score"] = tfa.metrics.F1Score(
            num_classes=self._num_tags - 1,  # `0` prediction is not a prediction
            average="micro",
        )

    def set_training_phase(self, training: bool) -> None:
        if training:
            self.training = tf.ones((), tf.bool)
        else:
            self.training = tf.zeros((), tf.bool)

    def _combine_sparse_dense_features(
        self,
        features: List[Union[tf.Tensor, tf.SparseTensor]],
        mask: tf.Tensor,
        name: Text,
        sparse_dropout: bool = False,
    ) -> tf.Tensor:

        dense_features = []

        for f in features:
            if isinstance(f, tf.SparseTensor):
                if sparse_dropout:
                    _f = self._tf_layers["sparse_dropout"](f, self.training)
                else:
                    _f = f

                dense_features.append(self._tf_layers["sparse_to_dense"][name](_f))
            else:
                dense_features.append(f)

        return tf.concat(dense_features, axis=-1) * mask

    def _create_bow(
        self,
        features: List[Union[tf.Tensor, "tf.SparseTensor"]],
        mask: tf.Tensor,
        name: Text,
    ) -> tf.Tensor:

        x = self._combine_sparse_dense_features(features, mask, name)
        return self._tf_layers["ffnn"][name](tf.reduce_sum(x, 1), self.training)

    def _create_sequence(
        self,
        features: List[Union[tf.Tensor, "tf.SparseTensor"]],
        mask: tf.Tensor,
        name: Text,
        masked_lm_loss: bool = False,
    ):
        x = self._combine_sparse_dense_features(
            features, mask, name, sparse_dropout=self.config[SPARSE_INPUT_DROPOUT]
        )

        if masked_lm_loss:
            pre, lm_mask_bool = self._tf_layers["input_mask"](x, mask, self.training)
        else:
            pre, lm_mask_bool = (x, None)

        transformed = self._tf_layers["transformer"](pre, 1 - mask, self.training)
        transformed = tf.nn.relu(transformed)

        return transformed, x, lm_mask_bool

    def _mask_loss(self, a_transformed, a, lm_mask_bool, name):
        # make sure there is at least one element in the mask
        lm_mask_bool = tf.cond(
            tf.reduce_any(lm_mask_bool),
            lambda: lm_mask_bool,
            lambda: tf.scatter_nd([[0, 0, 0]], [True], tf.shape(lm_mask_bool)),
        )

        lm_mask_bool = tf.squeeze(lm_mask_bool, -1)
        a_t_masked = tf.boolean_mask(a_transformed, lm_mask_bool)
        a_masked = tf.boolean_mask(a, lm_mask_bool)

        a_t_masked_embed = self._tf_layers["embed"][f"{name}_mask"](a_t_masked)
        a_masked_embed = self._tf_layers["embed"][f"{name}_token"](a_masked)

        return self._loss_mask(
            a_t_masked_embed, a_masked_embed, a_masked, a_masked_embed, a_masked
        )

    def _build_all_b(self):
        all_labels = self._create_bow(
            self.tf_label_data["label_features"],
            self.tf_label_data["label_mask"][0],
            "label",
        )
        all_labels_embed = self._tf_layers["embed"]["label"](all_labels)

        return all_labels_embed, all_labels

    def _intent_loss(self, a: tf.Tensor, b: tf.Tensor) -> tf.Tensor:
        all_labels_embed, all_labels = self._build_all_b()

        a_embed = self._tf_layers["embed"]["text"](a)
        b_embed = self._tf_layers["embed"]["label"](b)

        return self._tf_layers["loss_label"](
            a_embed, b_embed, b, all_labels_embed, all_labels
        )

    def _entity_loss(
        self, a: tf.Tensor, c: tf.Tensor, mask: tf.Tensor, sequence_lengths
    ) -> Tuple[tf.Tensor, tf.Tensor]:

        # remove cls token
        sequence_lengths = sequence_lengths - 1
        c = tf.cast(c[:, :, 0], tf.int32)
        logits = self._tf_layers["embed"]["logits"](a)

        loss = self._tf_layers["crf"].loss(logits, c, sequence_lengths)
        pred_ids = self._tf_layers["crf"](logits, sequence_lengths)

        # TODO check that f1 calculation is correct
        # calculate f1 score for train predictions
        mask_bool = tf.cast(mask[:, :, 0], tf.bool)
        # pick only non padding values and flatten sequences
        c_masked = tf.boolean_mask(c, mask_bool)
        pred_ids_masked = tf.boolean_mask(pred_ids, mask_bool)
        # set `0` prediction to not a prediction
        c_masked_1 = tf.one_hot(c_masked - 1, self._num_tags - 1)
        pred_ids_masked_1 = tf.one_hot(pred_ids_masked - 1, self._num_tags - 1)

        f1 = self._tf_layers["crf_f1_score"](c_masked_1, pred_ids_masked_1)

        return loss, f1

    def _train_losses_scores(
        self, batch_in: Union[Tuple[np.ndarray], Tuple[tf.Tensor]]
    ) -> None:
        tf_batch_data = self.batch_to_model_data_format(batch_in, self.data_signature)

        mask_text = tf_batch_data["text_mask"][0]
        sequence_lengths = tf.cast(tf.reduce_sum(mask_text[:, :, 0], 1), tf.int32)

        text_transformed, text_in, lm_mask_bool_text = self._create_sequence(
            tf_batch_data["text_features"], mask_text, "text", self.config[MASKED_LM]
        )

        if self.config[MASKED_LM]:
            loss, acc = self._mask_loss(
                text_transformed, text_in, lm_mask_bool_text, "text"
            )
            self.mask_loss.update_state(loss)
            self.mask_acc.update_state(acc)

        if self.config[INTENT_CLASSIFICATION]:
            # get _cls_ vector for intent classification
            last_index = tf.maximum(
                tf.constant(0, dtype=sequence_lengths.dtype), sequence_lengths - 1
            )
            idxs = tf.stack([tf.range(tf.shape(last_index)[0]), last_index], axis=1)
            cls = tf.gather_nd(text_transformed, idxs)

            label = self._create_bow(
                tf_batch_data["label_features"], tf_batch_data["label_mask"][0], "label"
            )
            loss, acc = self._intent_loss(cls, label)
            self.intent_loss.update_state(loss)
            self.intent_acc.update_state(acc)

        if self.config[ENTITY_RECOGNITION]:
            tags = tf_batch_data["tag_ids"][0]

            loss, f1 = self._entity_loss(
                text_transformed, tags, mask_text, sequence_lengths
            )
            self.entity_loss.update_state(loss)
            self.entity_f1.update_state(f1)

    def build_for_predict(self, model_data: RasaModelData) -> None:
        self.batch_tuple_sizes = model_data.batch_tuple_sizes()

        all_labels_embed, _ = self._build_all_b()
        self.all_labels_embed = tf.constant(all_labels_embed.numpy())

    def predict(
        self, batch_in: Union[Tuple[np.ndarray], Tuple[tf.Tensor]], **kwargs
    ) -> Dict[Text, tf.Tensor]:
        tf_batch_data = self.batch_to_model_data_format(batch_in, self.data_signature)

        mask_text = tf_batch_data["text_mask"][0]
        sequence_lengths = tf.cast(tf.reduce_sum(mask_text[:, :, 0], 1), tf.int32)

        text_transformed, _, _ = self._create_sequence(
            tf_batch_data["text_features"], mask_text, "text"
        )

        out = {}
        if self.config[INTENT_CLASSIFICATION]:
            # get _cls_ vector for intent classification
            last_index = tf.maximum(
                tf.constant(0, dtype=sequence_lengths.dtype), sequence_lengths - 1
            )
            idxs = tf.stack([tf.range(tf.shape(last_index)[0]), last_index], axis=1)
            cls = tf.gather_nd(text_transformed, idxs)
            cls_embed = self._embed["text"](cls)

            sim_all = self._loss_label.sim(
                cls_embed[:, tf.newaxis, :], self.all_labels_embed[tf.newaxis, :, :]
            )

            scores = train_utils.confidence_from_sim(
                sim_all, self.config[SIMILARITY_TYPE]
            )
            out["i_scores"] = scores

        if self.config[ENTITY_RECOGNITION]:
            logits = self._embed["logits"](text_transformed)
            pred_ids = self._crf(logits, sequence_lengths - 1)
            out["e_ids"] = pred_ids

        return out

    @staticmethod
    def _create_sparse_dense_layer(
        data_signature: List[FeatureSignature],
        name: Text,
        reg_lambda: float,
        dense_dim: int,
    ) -> Optional[tf_layers.DenseForSparse]:

        sparse = False
        for is_sparse, shape in data_signature:
            if is_sparse:
                sparse = is_sparse
            else:
                # if dense features are present
                # use the feature dimension of the dense features
                dense_dim = shape[-1]

        if sparse:
            return tf_layers.DenseForSparse(
                units=dense_dim, reg_lambda=reg_lambda, name=name
            )

    @staticmethod
    def _input_dim(data_signature: List[FeatureSignature], dense_dim: int) -> int:

        for is_sparse, shape in data_signature:
            if not is_sparse:
                # if dense features are present
                # use the feature dimension of the dense features
                dense_dim = shape[-1]
                break

        return dense_dim * len(data_signature)
