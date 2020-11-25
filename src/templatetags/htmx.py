from django import template
from django.template import TemplateSyntaxError
from django.template.base import token_kwargs
from django.template.loader_tags import construct_relative_path, IncludeNode
from django.template import Context
from django.utils.safestring import mark_safe

register = template.Library()


class HTMXIncludeNode(IncludeNode):
    def render(self, context):
        rendered = super().render(context)
        t = context.template.engine.get_template('htmx/wrapper.html')
        return t.render(Context({'content': rendered, 'replace_id': context.get('replace_id', 'htmx-replace')}))


@register.tag('htmx_include')
def do_include(parser, token):
    """
    Load a template and render it with the current context. You can pass
    additional context using keyword arguments.
    Example::
        {% htmx_include "foo/some_include" %}
        {% htmx_include "foo/some_include" with bar="BAZZ!" baz="BING!" %}
    Use the ``only`` argument to exclude the current context when rendering
    the included template::
        {% htmx_include "foo/some_include" only %}
        {% htmx_include "foo/some_include" with bar="1" only %}
    """
    bits = token.split_contents()
    if len(bits) < 2:
        raise TemplateSyntaxError(
            "%r tag takes at least one argument: the name of the template to "
            "be included." % bits[0]
        )
    options = {}
    remaining_bits = bits[2:]
    while remaining_bits:
        option = remaining_bits.pop(0)
        if option in options:
            raise TemplateSyntaxError('The %r option was specified more '
                                      'than once.' % option)
        if option == 'with':
            value = token_kwargs(remaining_bits, parser, support_legacy=False)
            if not value:
                raise TemplateSyntaxError('"with" in %r tag needs at least '
                                          'one keyword argument.' % bits[0])
        elif option == 'only':
            value = True
        else:
            raise TemplateSyntaxError('Unknown argument for %r tag: %r.' %
                                      (bits[0], option))
        options[option] = value
    isolated_context = options.get('only', False)
    namemap = options.get('with', {})
    bits[1] = construct_relative_path(parser.origin.template_name, bits[1])
    return HTMXIncludeNode(parser.compile_filter(bits[1]), extra_context=namemap,
                       isolated_context=isolated_context)


def tuple_to_html_attr(attr):
    return f'{attr[0]}="{attr[1]}"'


def extract_truthy(attr):
    return attr[1]


@register.simple_tag(takes_context=True)
def htmx_attrs(context, *args, **kwargs):
    swap = kwargs.get('swap', 'innerHTML')
    push_url = kwargs.get('push_url', False)

    attrs = {
        'hx-target': f"#{context.get('replace_id')}",
        'hx-swap': swap,
        'hx-push-url': 'true' if push_url else False,
    }

    return mark_safe(' '.join(map(tuple_to_html_attr, filter(extract_truthy, list(attrs.items())))))
