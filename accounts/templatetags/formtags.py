from django import template

register = template.Library()


@register.filter
def add_class(field, css):
    """Render a bound form field with an extra CSS class on its widget."""
    existing = field.field.widget.attrs.get('class', '')
    classes = (existing + ' ' + css).strip()
    return field.as_widget(attrs={'class': classes})


@register.filter
def field_type(field):
    return field.field.widget.__class__.__name__


@register.filter
def lookup(mapping, key):
    """dict[key] from a template — used to keep a filter dropdown's current
    selection when the admin list page re-renders."""
    try:
        return mapping.get(key, '')
    except AttributeError:
        return ''
