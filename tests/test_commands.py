import asyncio
from importlib.util import find_spec

from prompt_toolkit.document import Document
from prompt_toolkit.completion import CompleteEvent


def api():
    assert find_spec('mole_agent.commands'), 'command registry is not implemented'
    from mole_agent.commands import CommandRegistry, CommandSpec, Message, dispatch, SlashCompleter
    return CommandRegistry, CommandSpec, Message, dispatch, SlashCompleter


def test_registration_completion_help_and_dispatch():
    Registry, Spec, Message, dispatch, Completer = api()
    calls = []
    async def handler(ctx, args):
        calls.append((ctx, args))
        return Message(args)
    registry = Registry()
    registry.register(Spec('foo', '示例', handler, aliases=('f',)))
    completer = Completer(registry, lambda: 'current')
    items = list(completer.get_completions(Document('/f'), CompleteEvent()))
    assert [c.text for c in items] == ['/foo']
    assert not calls
    assert '/foo' in registry.help_text()
    result = asyncio.run(dispatch(registry, 'current', '/f  hello "world"'))
    assert result.text == 'hello "world"'
    assert calls == [('current', 'hello "world"')]
    import pytest
    with pytest.raises(ValueError):
        registry.register(Spec('f', 'collision', handler))
    assert list(completer.get_completions(Document('path /f'), CompleteEvent())) == []


def test_availability_and_unknown_never_execute():
    Registry, Spec, Message, dispatch, _ = api()
    async def handler(ctx, args):
        raise AssertionError('must not execute')
    registry = Registry()
    registry.register(Spec('locked', '锁定', handler, availability=lambda ctx: '运行中'))
    assert '运行中' in asyncio.run(dispatch(registry, None, '/locked')).text
    assert '未知命令' in asyncio.run(dispatch(registry, None, '/missing')).text


def test_real_prompt_confirmation_and_escape():
    async def scenario():
        from prompt_toolkit import PromptSession
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from mole_agent.commands import SlashCompleter, default_registry, command_key_bindings
        with create_pipe_input() as pipe:
            session = PromptSession(input=pipe, output=DummyOutput(),
                                    completer=SlashCompleter(default_registry(), lambda: None),
                                    complete_while_typing=True, key_bindings=command_key_bindings())
            task = asyncio.create_task(session.prompt_async('> '))
            pipe.send_text('/ne')
            for _ in range(100):
                if session.default_buffer.complete_state:
                    break
                await asyncio.sleep(.01)
            assert session.default_buffer.complete_state is not None
            assert session.default_buffer.complete_state.current_completion is None
            pipe.send_text('\t')
            await asyncio.sleep(.05)
            assert session.default_buffer.text == '/new'
            pipe.send_text('\r')
            await asyncio.sleep(.05)
            assert not task.done(), 'confirmation must not submit'
            pipe.send_text('\r')
            assert await asyncio.wait_for(task, 2) == '/new'
    asyncio.run(scenario())


def test_repl_commands_keep_behavior(tmp_path):
    from types import SimpleNamespace
    from mole_agent.cli import Repl
    from mole_agent.config import Settings

    class Usage:
        def reset(self):
            self.was_reset = True
        def summary(self):
            return dict(model_calls=1, input_tokens=2, output_tokens=3, total_tokens=5)
    usage = Usage()
    bundle = SimpleNamespace(usage=usage, approval=SimpleNamespace(always_allow={'bash'}),
                             subagents={})
    repl = Repl(Settings(home_dir=tmp_path), bundle)
    previous = repl.session_id
    asyncio.run(repl.handle_slash('/new'))
    assert repl.session_id != previous
    assert usage.was_reset and not bundle.approval.always_allow
    assert '补充要求：检查边界' in asyncio.run(repl.handle_slash('/review 检查边界'))
    assert asyncio.run(repl.handle_slash('/missing')) is None
    import pytest
    with pytest.raises(EOFError):
        asyncio.run(repl.handle_slash('/q'))


def test_arguments_preserve_suffix_and_async_completions():
    from mole_agent.commands import CommandRegistry, CommandSpec, CompletionItem, Message, SlashCompleter
    async def handler(ctx, args):
        return Message(args)
    async def complete(ctx, prefix):
        return [CompletionItem('provider/model', description='本地配置')]
    registry = CommandRegistry()
    registry.register(CommandSpec('model', '模型', handler, complete_args=complete))
    completer = SlashCompleter(registry, lambda: None)
    async def collect():
        return [item async for item in completer.get_completions_async(
            Document('/model pro'), CompleteEvent())]
    items = asyncio.run(collect())
    assert items[0].start_position == -3
    assert items[0].text == 'provider/model'
    assert list(completer.get_completions(Document('/model pro', cursor_position=9), CompleteEvent())) == []


def test_dispatch_errors_and_cancellation():
    import pytest
    from mole_agent.commands import CommandRegistry, CommandSpec, dispatch
    async def fail(ctx, args):
        raise RuntimeError('failed')
    async def cancel(ctx, args):
        raise asyncio.CancelledError()
    registry = CommandRegistry()
    registry.register(CommandSpec('fail', '失败', fail))
    registry.register(CommandSpec('cancel', '取消', cancel))
    assert 'failed' in asyncio.run(dispatch(registry, None, '/fail')).text
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dispatch(registry, None, '/cancel'))


def test_escape_preserves_input():
    async def scenario():
        from prompt_toolkit import PromptSession
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from mole_agent.commands import SlashCompleter, default_registry, command_key_bindings
        with create_pipe_input() as pipe:
            session = PromptSession(input=pipe, output=DummyOutput(),
                                    completer=SlashCompleter(default_registry(), lambda: None),
                                    complete_while_typing=True, key_bindings=command_key_bindings())
            task = asyncio.create_task(session.prompt_async('> '))
            pipe.send_text('/mo')
            for _ in range(100):
                if session.default_buffer.complete_state:
                    break
                await asyncio.sleep(.01)
            assert session.default_buffer.complete_state is not None
            session.app.ttimeoutlen = .01
            pipe.send_text('\x1b')
            for _ in range(100):
                if session.default_buffer.complete_state is None:
                    break
                await asyncio.sleep(.01)
            assert session.default_buffer.complete_state is None
            assert session.default_buffer.text == '/mo'
            assert not task.done()
            pipe.send_text('\r')
            assert await asyncio.wait_for(task, 2) == '/mo'
    asyncio.run(scenario())
