import inspect
import os
import sys
import unittest
from pathlib import Path

import torch
import torch_xla.core.xla_model as xm
from torch.utils.data import DataLoader

from accelerate import Accelerator
from accelerate.config import DistributedState
from accelerate.data_loader import prepare_data_loader
from accelerate.gather import gather
from accelerate.utils import set_seed, synchronize_rng_states
from testing_utils import are_the_same_tensors, execute_subprocess_async
from training_utils import RegressionDataset, RegressionModel


class MultiTPUTester(unittest.TestCase):
    def setUp(self):
        self.test_file_path = inspect.getfile(self.__class__)
        self.test_dir = Path(self.test_file_path).parent

    def test_tpu(self):
        distributed_args = f"""
            {self.test_dir}/xla_spawn.py
            --num_cores 8
            {self.test_file_path}
        """.split()
        cmd = [sys.executable] + distributed_args
        execute_subprocess_async(cmd, env=os.environ.copy())


def init_state_check():
    # Test we can instantiate this twice in a row.
    state = DistributedState()
    if state.process_index == 0:
        print("Testing, testing. 1, 2, 3.")
    print(state)


def rng_sync_check():
    state = DistributedState()
    synchronize_rng_states()
    assert are_the_same_tensors(torch.get_rng_state())
    if state.process_index == 0:
        print("All rng are properly synched.")


def dl_preparation_check():
    dl = DataLoader(range(256), batch_size=8)
    state = DistributedState()
    dl = prepare_data_loader(dl, state.device, state.num_processes, state.process_index, put_on_device=True)
    result = []
    for batch in dl:
        result.append(gather(batch))
    result = torch.cat(result)
    assert result.cpu().tolist() == list(range(256))
    if state.process_index == 0:
        print("Non-shuffled dataloader passing.")

    dl = DataLoader(range(256), batch_size=8, shuffle=True)
    dl = prepare_data_loader(dl, state.device, state.num_processes, state.process_index, put_on_device=True)
    result = []
    for batch in dl:
        result.append(gather(batch))
    result = torch.cat(result).tolist()
    result.sort()
    assert result == list(range(256))
    if state.process_index == 0:
        print("Shuffled dataloader passing.")


def mock_training():
    set_seed(42)
    train_set = RegressionDataset()
    train_dl = DataLoader(train_set, batch_size=16, shuffle=True)
    model = RegressionModel()
    optimizer = torch.optim.SGD(model.parameters(), lr=0.1)
    model.train()
    for _ in range(3):
        for batch in train_dl:
            model.zero_grad()
            output = model(batch["x"])
            loss = torch.nn.functional.mse_loss(output, batch["y"])
            loss.backward()
            optimizer.step()
    return train_set, model


def training_check():
    train_set, old_model = mock_training()
    assert are_the_same_tensors(old_model.a)
    assert are_the_same_tensors(old_model.b)

    accelerator = Accelerator(put_objects_on_device=True)
    train_dl = DataLoader(train_set, batch_size=2, shuffle=True)
    model = RegressionModel()
    optimizer = torch.optim.SGD(model.parameters(), lr=0.1)

    train_dl, model, optimizer = accelerator.prepare(train_dl, model, optimizer)
    model.train()
    set_seed(42)
    for _ in range(3):
        for batch in train_dl:
            optimizer.zero_grad()
            output = model(batch["x"])
            loss = torch.nn.functional.mse_loss(output, batch["y"])
            loss.backward()
            xm.optimizer_step(optimizer.optimizer)

    model = model.cpu()

    assert torch.allclose(old_model.a, model.a)
    assert torch.allclose(old_model.b, model.b)
    accelerator.print("Training yielded the same results on one CPU or 8 TPUs.")


def main():
    state = DistributedState()
    if state.process_index == 0:
        print("**Initialization**")
    init_state_check()

    if state.process_index == 0:
        print("\n**Test random number generator synchronization**")
    rng_sync_check()

    if state.process_index == 0:
        print("\n**DataLoader integration test**")
    dl_preparation_check()

    if state.process_index == 0:
        print("\n**Training integration test**")
    training_check()


def _mp_fn(index):
    # For xla_spawn (TPUs)
    main()


if __name__ == "__main__":
    main()
