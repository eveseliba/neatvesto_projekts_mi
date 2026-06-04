import os, logging, sys
os.environ.setdefault('PYCARET_CUSTOM_LOGGING_PATH', '/dev/null')
from pycaret.classification import setup, create_model, tune_model, finalize_model, save_model

RANDOM_STATE_ID = 42

def _stdout_logger():
    logger = logging.getLogger('pycaret_stdout')
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter('%(asctime)s %(name)s %(levelname)s %(message)s'))
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
    return logger

def setup_pycaret(df, target: str, categorical_features: list, ignore_features: list):
    print("setup_pycaret")
    return setup(
        data=df,
        target=target,
        categorical_features=categorical_features,
        ignore_features=ignore_features,
        fix_imbalance=True,
        normalize=True,
        normalize_method='robust',
        remove_outliers=True,
        outliers_threshold=0.05,
        session_id=RANDOM_STATE_ID,
        verbose=False,
    )


def train_best_model():
    return create_model('lightgbm', verbose=False, fold=3)


def tune_best_model(model):
    print("tune_best_model")
    return tune_model(model, optimize='AUC', n_iter=10, verbose=False)
