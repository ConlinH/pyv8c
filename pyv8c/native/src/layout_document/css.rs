use std::collections::HashMap;
use taffy::prelude::*;
use taffy::{BoxSizing, GridAutoFlow, Overflow, Point};

pub type Properties = HashMap<String, String>;

#[repr(align(8))]
pub struct Calculation {
    pixels: f64,
    ratio: f64,
}

pub fn resolve_calc(handle: *const (), basis: f32) -> f32 {
    // The style's owning node keeps this immutable allocation alive during the solve.
    let calc = unsafe { &*handle.cast::<Calculation>() };
    let result = ((calc.pixels + calc.ratio * f64::from(basis)) * 64.0).trunc() / 64.0;
    result.clamp(0.0, f64::from(f32::MAX)) as f32
}

pub fn get<'a>(values: &'a Properties, name: &str) -> &'a str {
    values.get(name).map(String::as_str).unwrap_or("")
}

pub fn number(value: &str) -> Option<f32> {
    value
        .trim()
        .parse::<f32>()
        .ok()
        .filter(|value| value.is_finite())
}

pub fn pixels(value: &str, font: f32, root: f32) -> Option<f32> {
    let value = value.trim();
    for (suffix, scale) in [
        ("rem", root),
        ("px", 1.0),
        ("em", font),
        ("pt", 96.0 / 72.0),
    ] {
        if let Some(value) = value.strip_suffix(suffix) {
            return number(value)
                .map(|v| (v * scale * 64.0).trunc() / 64.0)
                .filter(|v| v.is_finite());
        }
    }
    number(value)
}

fn percentage(value: &str) -> Option<f32> {
    number(value.trim().strip_suffix('%')?).map(|v| v / 100.0)
}

fn alignment(value: &str) -> Option<AlignItems> {
    Some(match value {
        "start" | "self-start" => AlignItems::START,
        "end" | "self-end" => AlignItems::END,
        "flex-start" => AlignItems::FLEX_START,
        "flex-end" => AlignItems::FLEX_END,
        "center" => AlignItems::CENTER,
        "baseline" => AlignItems::BASELINE,
        "stretch" => AlignItems::STRETCH,
        _ => return None,
    })
}

fn justification(value: &str) -> Option<AlignContent> {
    Some(match value {
        "start" | "left" => AlignContent::START,
        "end" | "right" => AlignContent::END,
        "flex-start" => AlignContent::FLEX_START,
        "flex-end" => AlignContent::FLEX_END,
        "center" => AlignContent::CENTER,
        "stretch" => AlignContent::STRETCH,
        "space-between" => AlignContent::SPACE_BETWEEN,
        "space-around" => AlignContent::SPACE_AROUND,
        "space-evenly" => AlignContent::SPACE_EVENLY,
        _ => return None,
    })
}

fn overflow(value: &str) -> Overflow {
    match value {
        "hidden" => Overflow::Hidden,
        "clip" => Overflow::Clip,
        "scroll" | "auto" => Overflow::Scroll,
        _ => Overflow::Visible,
    }
}

fn shorthand(value: &str) -> [&str; 4] {
    let mut parts = value.split_ascii_whitespace();
    let top = parts.next().unwrap_or("0");
    let right = parts.next().unwrap_or(top);
    let bottom = parts.next().unwrap_or(top);
    [top, right, bottom, parts.next().unwrap_or(right)]
}

fn edges<T>(
    values: &Properties,
    prefix: &str,
    suffix: &str,
    convert: impl Fn(&str) -> T,
) -> Rect<T> {
    let full_name = format!("{prefix}{suffix}");
    let defaults = shorthand(get(values, &full_name));
    let side = |name: &str, index| {
        if prefix == "border" {
            let side_style = ["border-", name, "-style"].concat();
            let border_style = values
                .get(&side_style)
                .map_or(get(values, "border-style"), String::as_str);
            if matches!(border_style, "none" | "hidden") {
                return convert("0");
            }
        }
        let key = [prefix, "-", name, suffix].concat();
        convert(values.get(&key).map_or(defaults[index], String::as_str))
    };
    Rect {
        top: side("top", 0),
        right: side("right", 1),
        bottom: side("bottom", 2),
        left: side("left", 3),
    }
}

// Keep whitespace inside CSS functions together when reading tracks.
fn components(value: &str) -> impl Iterator<Item = &str> {
    let mut depth = 0i32;
    value
        .split(move |ch: char| {
            match ch {
                '(' => depth += 1,
                ')' => depth -= 1,
                _ => (),
            }
            depth == 0 && ch.is_ascii_whitespace()
        })
        .filter(|part| !part.is_empty())
}

fn tracks(value: &str, font: f32, root: f32) -> Vec<GridTemplateComponent<String>> {
    fn track(value: &str, font: f32, root: f32) -> TrackSizingFunction {
        if let Some((min, max)) = value
            .strip_prefix("minmax(")
            .and_then(|v| v.strip_suffix(')'))
            .and_then(|v| v.split_once(','))
        {
            return minmax(
                track(min.trim(), font, root).min,
                track(max.trim(), font, root).max,
            );
        }
        if let Some(ratio) = value.strip_suffix("fr").and_then(number) {
            return fr(ratio.max(0.0));
        }
        if let Some(ratio) = percentage(value) {
            return percent(ratio);
        }
        if let Some(size) = pixels(value, font, root) {
            return length(size.max(0.0));
        }
        match value {
            "min-content" => min_content(),
            "max-content" => max_content(),
            _ => auto(),
        }
    }
    components(value)
        .filter(|v| *v != "none" && !v.starts_with('['))
        .flat_map(|value| {
            if let Some((count, values)) = value
                .strip_prefix("repeat(")
                .and_then(|v| v.strip_suffix(')'))
                .and_then(|v| v.split_once(','))
            {
                let count = count.trim().parse::<usize>().unwrap_or(1).min(256);
                let values: Vec<_> = components(values)
                    .map(|v| GridTemplateComponent::Single(track(v, font, root)))
                    .collect();
                return values
                    .iter()
                    .cloned()
                    .cycle()
                    .take(count * values.len())
                    .collect();
            }
            vec![GridTemplateComponent::Single(track(value, font, root))]
        })
        .take(1024)
        .collect()
}

fn grid_line(value: &str) -> GridPlacement<String> {
    if let Some(count) = value
        .trim()
        .strip_prefix("span ")
        .and_then(|v| v.parse::<u16>().ok())
    {
        return span(count.clamp(1, 1024));
    }
    value
        .trim()
        .parse::<i16>()
        .ok()
        .filter(|line| *line != 0)
        .map_or_else(auto, |value| line(value.clamp(-1024, 1024)))
}

pub fn layout_style(
    values: &Properties,
    root: f32,
    aspect_ratio: Option<f32>,
    calculations: &mut Vec<Box<Calculation>>,
) -> Style {
    let read = |name: &str| get(values, name);
    let font = pixels(read("font-size"), 16.0, root).unwrap_or(16.0);
    let length_auto = |value: &str| {
        percentage(value)
            .map(percent)
            .or_else(|| pixels(value, font, root).map(length))
            .unwrap_or_else(auto)
    };
    let edge = |value: &str| {
        percentage(value)
            .map(percent)
            .or_else(|| pixels(value, font, root).map(|v| length(v.max(0.0))))
            .unwrap_or_else(zero)
    };
    let mut dimension = |name: &str, value: &str| {
        if value.contains('%')
            && let Some(encoded) = values.get(&format!("-layout-calc-{name}"))
            && let Some((pixels, ratio)) = encoded.split_once(' ')
            && let (Ok(pixels), Ok(ratio)) = (pixels.parse::<f64>(), ratio.parse::<f64>())
            && pixels.is_finite()
            && ratio.is_finite()
        {
            let calc = Box::new(Calculation { pixels, ratio });
            let handle = std::ptr::from_ref(calc.as_ref()).cast();
            calculations.push(calc);
            return Dimension::calc(handle);
        }
        percentage(value)
            .map(percent)
            .or_else(|| pixels(value, font, root).map(|v| length(v.max(0.0))))
            .unwrap_or_else(auto)
    };
    let gap = shorthand(read("gap"));
    let overflow_both = shorthand(read("overflow"));
    let areas: Vec<_> = read("grid-area").split('/').map(str::trim).collect();
    let placement = |name: &str, start_name, end_name, axis| {
        let (start, end) = read(name).split_once('/').unwrap_or((read(name), ""));
        let choose = |key, shorthand: &str, index| {
            values
                .get(key)
                .map(String::as_str)
                .or_else(|| (!shorthand.trim().is_empty()).then_some(shorthand))
                .map(grid_line)
                .unwrap_or_else(|| grid_line(areas.get(index).copied().unwrap_or("")))
        };
        Line {
            start: choose(start_name, start, axis),
            end: choose(end_name, end, axis + 2),
        }
    };
    let mut flex = read("flex").split_ascii_whitespace();
    let flex_start = flex.next();
    let flex_second = flex.next();
    let flex_third = flex.next();
    let grow = number(read("flex-grow"))
        .or_else(|| flex_start.and_then(number))
        .unwrap_or(f32::from(matches!(flex_start, Some("auto"))));
    let shrink = number(read("flex-shrink"))
        .or_else(|| flex_second.and_then(number))
        .unwrap_or(if flex_start == Some("none") { 0.0 } else { 1.0 });
    let basis = values
        .get("flex-basis")
        .map(String::as_str)
        .or(flex_third)
        .or_else(|| flex_second.filter(|basis| number(basis).is_none()))
        .or_else(|| flex_start.filter(|basis| number(basis).is_none()))
        .unwrap_or(if flex_start.is_some() { "0%" } else { "auto" });
    let inset = if matches!(
        read("position"),
        "relative" | "absolute" | "fixed" | "sticky"
    ) {
        let offset = |value: &str| {
            percentage(value)
                .map(percent)
                .or_else(|| pixels(value, font, root).map(|v| length((v * 64.0).trunc() / 64.0)))
                .unwrap_or_else(auto)
        };
        Rect {
            top: offset(read("top")),
            right: offset(read("right")),
            bottom: offset(read("bottom")),
            left: offset(read("left")),
        }
    } else {
        auto()
    };
    Style {
        display: match read("display") {
            "none" => Display::None,
            "grid" | "inline-grid" => Display::Grid,
            "flex" | "inline-flex" => Display::Flex,
            _ => Display::Block,
        },
        position: if matches!(read("position"), "absolute" | "fixed") {
            Position::Absolute
        } else {
            Position::Relative
        },
        inset,
        box_sizing: if read("box-sizing") == "border-box" {
            BoxSizing::BorderBox
        } else {
            BoxSizing::ContentBox
        },
        size: Size {
            width: dimension("width", read("width")),
            height: dimension("height", read("height")),
        },
        min_size: Size {
            width: dimension("min-width", read("min-width")),
            height: dimension("min-height", read("min-height")),
        },
        max_size: Size {
            width: dimension("max-width", read("max-width")),
            height: dimension("max-height", read("max-height")),
        },
        aspect_ratio: read("aspect-ratio")
            .split_once('/')
            .and_then(|(a, b)| Some(number(a)? / number(b)?))
            .or_else(|| number(read("aspect-ratio")))
            .filter(|v| v.is_finite() && *v > 0.0)
            .or(aspect_ratio),
        margin: edges(values, "margin", "", length_auto),
        padding: edges(values, "padding", "", edge),
        border: edges(values, "border", "-width", edge),
        overflow: Point {
            x: overflow(
                values
                    .get("overflow-x")
                    .map_or(overflow_both[0], String::as_str),
            ),
            y: overflow(
                values
                    .get("overflow-y")
                    .map_or(overflow_both[1], String::as_str),
            ),
        },
        gap: Size {
            width: edge(values.get("column-gap").map_or(gap[1], String::as_str)),
            height: edge(values.get("row-gap").map_or(gap[0], String::as_str)),
        },
        flex_direction: match read("flex-direction") {
            "column" => FlexDirection::Column,
            "column-reverse" => FlexDirection::ColumnReverse,
            "row-reverse" => FlexDirection::RowReverse,
            _ => FlexDirection::Row,
        },
        flex_wrap: match read("flex-wrap") {
            "wrap" => FlexWrap::Wrap,
            "wrap-reverse" => FlexWrap::WrapReverse,
            _ => FlexWrap::NoWrap,
        },
        flex_grow: grow.max(0.0),
        flex_shrink: shrink.max(0.0),
        flex_basis: dimension("flex-basis", basis),
        align_items: alignment(read("align-items")),
        align_self: alignment(read("align-self")),
        justify_items: alignment(read("justify-items")),
        justify_self: alignment(read("justify-self")),
        align_content: justification(read("align-content")),
        justify_content: justification(read("justify-content")),
        grid_template_columns: tracks(read("grid-template-columns"), font, root),
        grid_template_rows: tracks(read("grid-template-rows"), font, root),
        grid_auto_flow: match read("grid-auto-flow") {
            "column" => GridAutoFlow::Column,
            "dense" | "row dense" => GridAutoFlow::RowDense,
            "column dense" => GridAutoFlow::ColumnDense,
            _ => GridAutoFlow::Row,
        },
        grid_column: placement("grid-column", "grid-column-start", "grid-column-end", 1),
        grid_row: placement("grid-row", "grid-row-start", "grid-row-end", 0),
        ..Style::default()
    }
}
