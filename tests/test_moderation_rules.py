from app.moderation_middleware import ACTION_WEIGHT, _matches


def test_action_priority_order() -> None:
    assert ACTION_WEIGHT["ban"] > ACTION_WEIGHT["mute"]
    assert ACTION_WEIGHT["mute"] > ACTION_WEIGHT["warning"]
    assert ACTION_WEIGHT["warning"] > ACTION_WEIGHT["delete"]


def test_whole_word_match_respects_boundaries() -> None:
    assert _matches("это тестпред сегодня", "тестпред", "whole", False)
    assert not _matches("это тестпредикат сегодня", "тестпред", "whole", False)


def test_phrase_contains_match() -> None:
    assert _matches("хочу купить рекламу сегодня", "купить рекламу", "contains", False)


def test_case_insensitive_match() -> None:
    assert _matches("КУПИТЬ РЕКЛАМУ", "купить рекламу", "contains", False)


def test_case_sensitive_match() -> None:
    assert _matches("КУПИТЬ РЕКЛАМУ", "КУПИТЬ РЕКЛАМУ", "contains", True)
    assert not _matches("КУПИТЬ РЕКЛАМУ", "купить рекламу", "contains", True)
