"""Seeded uniform selection over the same legal macro choices as Jev."""

import random

from .jev_agent import QUESTION


class RandomMacroClient:
    """Local decision client; does not construct an HTTP client or use a planner."""

    model = "uniform-random-legal-v1"

    def __init__(self, seed):
        self.seed = seed
        self.rng = random.Random(seed)

    def payload(self, state, choices):
        if not choices:
            raise ValueError("Random policy requires at least one legal action.")
        return {"model": self.model, "state": state,
                "policy": {"kind": "uniform_legal", "seed": self.seed, "includes_wait": True},
                "questions": {QUESTION: {"type": "choice",
                    "instructions": "Uniform random choice; observation is logged but not used to rank actions.",
                    "criteria": {str(k): v for k, v in choices.items()}}}}

    async def choose(self, payload):
        # Sorting stabilizes the random stream if a caller changes dict insertion order.
        choices = sorted(payload["questions"][QUESTION]["criteria"], key=int)
        probability = 1 / len(choices)
        return {"model": self.model,
                "answer": {"type": "choice", "choice": self.rng.choice(choices),
                           "confidence": probability,
                           "probabilities": {choice: probability for choice in choices}},
                "usage": {"input_tokens": 0, "output_tokens": 0}}

    async def close(self):
        pass
