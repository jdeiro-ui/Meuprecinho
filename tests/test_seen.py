from price_watch.db import init_db
from price_watch.telegram import _mark_seen


def test_mensagem_nao_repete_aviso():
    init_db()
    assert _mark_seen(-100777, 10) is True
    assert _mark_seen(-100777, 10) is False
    assert _mark_seen(-100777, 11) is True
    assert _mark_seen(-100888, 10) is True
