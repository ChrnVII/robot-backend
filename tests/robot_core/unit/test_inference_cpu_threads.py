import sys
from types import SimpleNamespace
import pytest
from app.robot_core.inference.heat_stress import HeatStressAdapter

pytestmark = pytest.mark.no_database


def test_cpu_budget_reapplied_after_backend_setup(tmp_path, monkeypatch):
    path = tmp_path / 'best.pt'
    path.write_bytes(b'fixture')
    pool = {'torch': 16, 'cv2': 8}
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(
        get_num_threads=lambda: pool['torch'], set_num_threads=lambda n: pool.update(torch=n)))
    monkeypatch.setitem(sys.modules, 'cv2', SimpleNamespace(
        getNumThreads=lambda: pool['cv2'], setNumThreads=lambda n: pool.update(cv2=n)))
    class Model:
        names = {0: 'heat'}
        def add_callback(self, event, callback):
            assert event == 'on_predict_start'
            self.callback = callback
        def predict(self, **kwargs):
            pool.update(torch=16)  # Ultralytics select_device initializes its default pool.
            self.callback(self)
            assert pool == {'torch': 4, 'cv2': 1}
            return []
    model = Model()
    monkeypatch.setitem(sys.modules, 'ultralytics', SimpleNamespace(YOLO=lambda _: model))
    monkeypatch.setattr('app.robot_core.inference.heat_stress.os.process_cpu_count', lambda: 8, raising=False)
    adapter = HeatStressAdapter(path, device='cpu', cpu_threads=4)
    adapter.load()
    adapter.warmup()
    assert pool == {'torch': 4, 'cv2': 1}


@pytest.mark.parametrize('threads', [0, -1, 1.5, True, 65])
def test_invalid_budget(tmp_path, threads):
    path = tmp_path / 'best.pt'
    path.write_bytes(b'fixture')
    with pytest.raises(ValueError, match='cpu_threads'):
        HeatStressAdapter(path, cpu_threads=threads)
