import inspect
import re
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.filters import has_completions


class SlashCompleter(Completer):
    def __init__(self, registry, context_factory):
        self.registry = registry
        self.context_factory = context_factory

    def _candidates(self, document):
        before = document.text_before_cursor
        # Avoid replacing a partial word while leaving its suffix behind.
        if not before.startswith("/") or (document.text_after_cursor and not document.text_after_cursor[0].isspace()):
            return [], 0
        match = re.match(r"^/([^\s]*)(\s+)?(.*)$", before, re.S)
        if not match:
            return [], 0
        name, separator, args = match.groups()
        ctx = self.context_factory()
        if not separator:
            items = []
            for spec in self.registry.match(name):
                reason = spec.availability(ctx) if spec.availability else None
                items.append(Completion("/" + spec.name, -len(before),
                                        display_meta=spec.description + (f"（{reason}）" if reason else "")))
            return items, 0
        spec = self.registry.resolve(name)
        if not spec or not spec.complete_args:
            return [], 0
        return spec.complete_args(ctx, args), len(args)

    def _convert(self, items, length):
        for item in items:
            if isinstance(item, Completion):
                yield item
            else:
                yield Completion(item.value, -length, display=item.label or item.value,
                                 display_meta=item.description)

    def get_completions(self, document, complete_event):
        items, length = self._candidates(document)
        if inspect.isawaitable(items):
            if inspect.iscoroutine(items):
                items.close()
            return
        yield from self._convert(items, length)

    async def get_completions_async(self, document, complete_event):
        items, length = self._candidates(document)
        if inspect.isawaitable(items):
            items = await items
        for item in self._convert(items, length):
            yield item


def command_key_bindings():
    bindings = KeyBindings()

    @bindings.add("enter", filter=has_completions)
    def confirm(event):
        buffer = event.current_buffer
        if buffer.complete_state and buffer.complete_state.current_completion:
            buffer.complete_state = None
        else:
            buffer.validate_and_handle()

    @bindings.add("escape", filter=has_completions)
    def dismiss(event):
        # Preserve text, including a preview selected with arrow keys.
        event.current_buffer.complete_state = None

    return bindings
