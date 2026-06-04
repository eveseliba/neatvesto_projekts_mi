from pycaret.classification import predict_model
from pycaret.utils.generic import check_metric


def evaluate_model(model, test_df):
    results = predict_model(model, data=test_df)
    # Identify prediction column
    cols = results.columns.tolist()
    pred_col = [c for c in cols if 'prediction' in c.lower() or 'label' in c.lower()][0]

    # Metrics to compute
    metrics = [
        'Accuracy', 'AUC', 'Recall', 'Precision', 'F1', 'Kappa', 'MCC'
    ]
    metric_values = {}
    for m in metrics:
        try:
            metric_values[m] = check_metric(
                test_df['appointment_attended'],
                results[pred_col],
                metric=m
            )
            print(f"{m}: {metric_values[m]:.4f}")
        except Exception as e:
            print(f"Error calculating {m}: {e}")
    return metric_values
