"""Smoke tests — fill in as modules land. Run: pytest -q"""
import generate_logs


def test_generator_is_deterministic():
    assert generate_logs.generate(50) == generate_logs.generate(50)


def test_generator_shape():
    e = generate_logs.generate(1)[0]
    assert set(e) == {"timestamp", "method", "path", "query", "request_body", "status", "response_body", "headers"}
