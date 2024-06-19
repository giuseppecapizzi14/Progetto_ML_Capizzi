import os

import torch.utils.data
from torch import Tensor
from torch.nn import CrossEntropyLoss, Module
from torch.optim import Optimizer
from torch.optim.lr_scheduler import LambdaLR, LRScheduler
from torch.utils.data import DataLoader
from tqdm import tqdm

from config.config import OPTIMIZERS, Config
from data_classes.emovo_dataset import EmovoDataset, Sample
from metrics import EvaluationMetric, Metrics, compute_metrics, evaluate
from model_classes.cnn_model import EmovoCNN


def train_one_epoch(
    model: Module,
    dataloader: DataLoader[Sample],
    loss_criterion: CrossEntropyLoss,
    optimizer: Optimizer,
    scheduler: LRScheduler,
    device: torch.device
) -> Metrics:
    model.train()
    running_loss = 0.0
    predictions: list[int] = []
    references: list[int] = []

    for batch in tqdm(dataloader, desc="Training"):
        waveforms: Tensor = batch["waveform"]
        waveforms = waveforms.to(device)

        labels: Tensor = batch["label"]
        labels = labels.to(device)

        optimizer.zero_grad()

        outputs = model(waveforms)
        loss: Tensor = loss_criterion(outputs, labels)
        loss.backward() # type: ignore
        optimizer.step()
        scheduler.step()

        running_loss += loss.item()

        pred = torch.argmax(outputs, dim = 1)
        predictions.extend(pred.cpu().numpy())
        references.extend(labels.cpu().numpy())

    return compute_metrics(predictions, references, running_loss, len(dataloader))

def manage_best_model_and_metrics(
    model: Module,
    evaluation_metric: EvaluationMetric,
    val_metrics: Metrics,
    best_val_metric: float,
    best_model: Module,
    lower_is_better: bool
) -> tuple[float, Module]:
    metric = val_metrics[evaluation_metric]

    if lower_is_better:
        is_best = metric <= best_val_metric
    else:
        is_best = metric > best_val_metric

    if is_best:
        print(f"New best model found with val {evaluation_metric}: {metric:.4f}")
        best_val_metric = metric
        best_model = model

    return best_val_metric, best_model


if __name__ == "__main__":
    # Legge il file di configurazione
    config = Config()

    device = config.training.device

    # Carica il dataset
    dataset = EmovoDataset(config.data.data_dir, resample=True)

    # Calcola le dimensioni dei dataset
    # |------- dataset -------|
    # |---train---|-val-|-test|
    dataset_size = len(dataset)

    train_size = int(config.data.train_ratio * dataset_size)

    test_val_size = dataset_size - train_size
    test_size = int(test_val_size * config.data.test_val_ratio)
    val_size = test_val_size - test_size

    train_dataset, test_dataset, val_dataset = torch.utils.data.random_split(dataset, [train_size, test_size, val_size])

    # Crea i DataLoader
    batch_size = config.training.batch_size
    train_dl = DataLoader(train_dataset, batch_size = batch_size, shuffle = True)
    test_dl = DataLoader(test_dataset, batch_size = batch_size, shuffle = False)
    val_dl = DataLoader(val_dataset, batch_size = batch_size, shuffle = False)

    # Crea il modello
    model = EmovoCNN(waveform_size = dataset.max_sample_len, dropout = config.model.dropout, device = device)
    model.to(device)

    # Definisce una funzione di loss
    criterion = CrossEntropyLoss()

    # Definisce un optimizer con il learning rate specificato
    optimizer = config.training.optimizer
    optimizer = OPTIMIZERS[optimizer]

    optimizer = optimizer(model.parameters(), lr = config.training.lr)

    # Definisce uno scheduler per il decay del learning rate
    epochs = config.training.epochs
    total_steps = len(train_dl) * epochs

    warmup_steps = int(total_steps * config.training.warmup_ratio)

    def lr_warmup_linear_decay(step: int):
        return (step / warmup_steps) if step < warmup_steps else max(0.0, (total_steps - step) / (total_steps - warmup_steps))

    scheduler = LambdaLR(optimizer, lr_lambda=lr_warmup_linear_decay)

    # Teniamo traccia del modello e della metrica migliore
    best_metric_lower_is_better = config.training.best_metric_lower_is_better
    best_val_metric = float("inf") if best_metric_lower_is_better else float("-inf")
    best_model = model

    # Stampa le informazioni sul processo di training
    print(f"Device: {device}")
    print(f"Train size: {len(train_dataset)}")
    print(f"Validation size: {len(val_dataset)}")
    print(f"Test size: {len(test_dataset)}")
    print()

    # Addestra il modello per il numero di epoche specificate
    evaluation_metric = config.training.evaluation_metric
    for epoch in range(epochs):
        print(f"Epoch {epoch + 1}/{epochs}")

        train_metrics = train_one_epoch(model, train_dl, criterion, optimizer, scheduler, device)
        val_metrics = evaluate(model, val_dl, criterion, device)

        train_loss = train_metrics["loss"]
        train_accuracy = train_metrics["accuracy"]
        val_loss = val_metrics["loss"]
        val_accuracy = val_metrics["accuracy"]

        print(f"Train loss: {train_loss:.4f} - Train accuracy: {train_accuracy:.4f}")
        print(f"Val loss: {val_loss:.4f} - Val accuracy: {val_accuracy:.4f}")

        best_val_metric, best_model = manage_best_model_and_metrics(
            model,
            evaluation_metric,
            val_metrics,
            best_val_metric,
            best_model,
            best_metric_lower_is_better
        )
        print()

    # Valuta le metriche del modello mediante il dataset di test
    test_metrics = evaluate(best_model, test_dl, criterion, device)
    for key, value in test_metrics.items():
        print(f"Test {key}: {value:.4f}")

    # Salva il modello
    checkpoint_dir = config.training.checkpoint_dir
    model_name = config.training.model_name
    os.makedirs(checkpoint_dir, exist_ok=True)
    torch.save(best_model.state_dict(), f"{checkpoint_dir}/{model_name}.pt") # type: ignore

    print("Model saved")
