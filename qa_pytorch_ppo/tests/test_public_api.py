import qa_pytorch_ppo
from qa_pytorch_ppo.environment import UnityQaEnvironment
from qa_pytorch_ppo.policy import ActorCritic, PpoPolicy
from qa_pytorch_ppo.ppo import PpoConfig, evaluate, train


def test_package_exports_the_documented_extension_points() -> None:
    assert qa_pytorch_ppo.UnityQaEnvironment is UnityQaEnvironment
    assert qa_pytorch_ppo.PpoPolicy is PpoPolicy
    assert qa_pytorch_ppo.ActorCritic is ActorCritic
    assert qa_pytorch_ppo.PpoConfig is PpoConfig
    assert qa_pytorch_ppo.train is train
    assert qa_pytorch_ppo.evaluate is evaluate
