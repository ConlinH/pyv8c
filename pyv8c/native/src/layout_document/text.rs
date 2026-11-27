use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, Mutex, OnceLock};

use skia_safe::font_arguments::{VariationPosition, variation_position::Coordinate};
use skia_safe::shaper::run_handler::{Buffer, RunHandler, RunInfo};
use skia_safe::{Data, Font, FontArguments, FontMgr, GlyphId, Point, Typeface};
use taffy::{AvailableSpace, Size};
use unicode_linebreak::{BreakOpportunity, linebreaks};
use unicode_segmentation::UnicodeSegmentation;

use super::css::{Properties, get, number, pixels};

pub type FontSet = Vec<(String, u32)>;

#[derive(Clone, Copy, Default)]
pub struct Metrics {
    pub size: Size<f32>,
    pub baseline: f32,
}

#[derive(Clone, Eq, Hash, PartialEq)]
struct ShapeKey {
    text: String,
    faces: FontSet,
    size: u32,
    weight: u32,
    rtl: bool,
}

#[derive(Default)]
struct Run {
    glyphs: Vec<GlyphId>,
    positions: Vec<Point>,
    clusters: Vec<u32>,
    advances: Vec<(usize, f32)>,
}

impl RunHandler for Run {
    fn begin_line(&mut self) {}
    fn run_info(&mut self, _info: &RunInfo) {}
    fn commit_run_info(&mut self) {}
    fn run_buffer(&mut self, info: &RunInfo) -> Buffer<'_> {
        self.glyphs.resize(info.glyph_count, 0);
        self.positions.resize(info.glyph_count, Point::default());
        self.clusters.resize(info.glyph_count, 0);
        let mut buffer = Buffer::new(&mut self.glyphs, &mut self.positions, None);
        buffer.clusters = Some(&mut self.clusters);
        buffer
    }
    fn commit_run_buffer(&mut self, info: &RunInfo) {
        for (index, (&cluster, position)) in self.clusters.iter().zip(&self.positions).enumerate() {
            let next = self
                .positions
                .get(index + 1)
                .map_or(info.advance.x, |point| point.x);
            self.advances.push((cluster as usize, next - position.x));
        }
    }
    fn commit_line(&mut self) {}
}

#[derive(Default)]
struct Shaped {
    advances: Vec<(usize, f32)>,
    ascent: f32,
    descent: f32,
    gap: f32,
}

#[derive(Default)]
pub struct TextCache {
    runs: HashMap<ShapeKey, Arc<Shaped>>,
    order: VecDeque<ShapeKey>,
}

type FaceCache = HashMap<(String, u32), Option<Typeface>>;
static FACES: OnceLock<Mutex<FaceCache>> = OnceLock::new();

pub struct Measured {
    parts: Vec<LinePart>,
    height: f32,
    baseline: f32,
}

struct LinePart {
    width: f32,
    trailing: f32,
    forced: bool,
    emergency_breaks: Vec<f32>,
}

impl Measured {
    pub fn strut(&self) -> Metrics {
        Metrics {
            size: Size {
                width: 0.0,
                height: self.height,
            },
            baseline: self.baseline,
        }
    }

    pub fn untrimmed_width(&self) -> f32 {
        self.parts.iter().map(|part| part.width).sum()
    }

    pub fn layout(&self, available: AvailableSpace) -> Metrics {
        let limit = match available {
            AvailableSpace::Definite(width) => width,
            AvailableSpace::MinContent => 0.0,
            AvailableSpace::MaxContent => f32::INFINITY,
        };
        let (mut line, mut lines, mut widest, mut trailing) = (0.0f32, 1, 0.0f32, 0.0f32);
        for part in &self.parts {
            let content = part.width - part.trailing;
            if line > 0.0 && line + content > limit {
                widest = widest.max(line - trailing);
                lines += 1;
                line = 0.0;
            }
            if matches!(available, AvailableSpace::Definite(_))
                && content > limit
                && !part.emergency_breaks.is_empty()
            {
                for &width in &part.emergency_breaks {
                    if line > 0.0 && line + width > limit {
                        widest = widest.max(line);
                        lines += 1;
                        line = 0.0;
                    }
                    line += width;
                }
                line += part.trailing;
            } else {
                line += part.width;
            }
            trailing = part.trailing;
            if part.forced {
                widest = widest.max(line - trailing);
                lines += 1;
                line = 0.0;
                trailing = 0.0;
            }
        }
        if self.parts.is_empty() || widest == 0.0 && line == trailing && lines == 1 {
            return Metrics::default();
        }
        Metrics {
            size: Size {
                width: (widest.max(line - trailing) * 64.0).ceil() / 64.0,
                height: self.height * lines as f32,
            },
            baseline: self.baseline,
        }
    }
}

impl TextCache {
    pub fn adjacent_widths(
        &mut self,
        parts: &[&str],
        style: &Properties,
        faces: &FontSet,
        root: f32,
    ) -> Vec<f32> {
        let size = font_size(style, root);
        let weight = number(get(style, "font-weight")).unwrap_or(400.0);
        let rtl = get(style, "direction") == "rtl";
        let shaped = self.shape(&parts.concat(), size, faces, rtl, weight);
        let letter = pixels(get(style, "letter-spacing"), size, root).unwrap_or(0.0);
        let word = pixels(get(style, "word-spacing"), size, root).unwrap_or(0.0);
        let mut advances = shaped.advances.iter().peekable();
        let mut end = 0;
        parts
            .iter()
            .map(|part| {
                end += part.len();
                let mut width = 0.0;
                while advances.peek().is_some_and(|(offset, _)| *offset < end) {
                    width += advances.next().unwrap().1;
                }
                let spacing =
                    letter * part.chars().count() as f32 + word * part.matches(' ').count() as f32;
                ((width + spacing) * 64.0).ceil() / 64.0
            })
            .collect()
    }

    fn typeface(face: &(String, u32)) -> Option<Typeface> {
        let cache = FACES.get_or_init(Mutex::default);
        if let Some(loaded) = cache.lock().unwrap().get(face).cloned() {
            return loaded;
        }
        let loaded = (|| {
            let data = std::fs::read(&face.0).ok()?;
            FontMgr::new().new_from_data(Data::new_copy(&data), face.1)
        })();
        let mut cache = cache.lock().unwrap();
        if cache.len() == 64 {
            cache.clear();
        }
        cache.entry(face.clone()).or_insert(loaded).clone()
    }

    fn shape(
        &mut self,
        text: &str,
        font_size: f32,
        faces: &FontSet,
        rtl: bool,
        weight: f32,
    ) -> Arc<Shaped> {
        let key = ShapeKey {
            text: text.to_owned(),
            faces: faces.clone(),
            size: font_size.to_bits(),
            weight: weight.to_bits(),
            rtl,
        };
        if let Some(run) = self.runs.get(&key) {
            return Arc::clone(run);
        }

        let mut fonts = Vec::new();
        for face in faces {
            if let Some(typeface) = Self::typeface(face) {
                let coordinates = [Coordinate {
                    axis: Coordinate::wght,
                    value: weight,
                }];
                let args =
                    FontArguments::default().set_variation_design_position(VariationPosition {
                        coordinates: &coordinates,
                    });
                let varied = typeface.clone_with_arguments(&args).unwrap_or(typeface);
                let mut font = Font::new(varied, font_size);
                font.set_subpixel(true).set_linear_metrics(true);
                fonts.push(font);
            }
        }
        let mut shaped = Shaped {
            ascent: font_size * 0.8,
            descent: font_size * 0.2,
            ..Shaped::default()
        };
        if let Some(font) = fonts.first() {
            (shaped.ascent, shaped.descent, shaped.gap) = font_extents(font, font_size);
        }
        let shaper = skia_safe::shapers::hb::shape_dont_wrap_or_reorder(None);
        let mut spans = Vec::<(usize, usize, Option<usize>)>::new();
        for (start, grapheme) in text.grapheme_indices(true) {
            let has_glyphs = |font: &Font| {
                grapheme.chars().all(|ch| {
                    ch.is_control()
                        || matches!(ch, '\u{200d}' | '\u{fe0e}' | '\u{fe0f}')
                        || font.unichar_to_glyph(ch as i32) != 0
                })
            };
            let emoji = grapheme
                .contains('\u{fe0f}')
                .then(|| {
                    fonts.iter().position(|font| {
                        has_glyphs(font)
                            && [b"COLR", b"CBDT", b"sbix", b"SVG "].iter().any(|name| {
                                font.typeface()
                                    .get_table_size(tag(name))
                                    .is_some_and(|size| size > 0)
                            })
                    })
                })
                .flatten();
            let font = emoji
                .or_else(|| fonts.iter().position(has_glyphs))
                .or_else(|| (!fonts.is_empty()).then_some(0));
            let end = start + grapheme.len();
            if let Some(previous) = spans.last_mut().filter(|span| span.2 == font) {
                previous.1 = end;
            } else {
                spans.push((start, end, font));
            }
        }
        for (start, end, face) in spans {
            let slice = &text[start..end];
            if let Some(font) = face.map(|index| &fonts[index]) {
                let (ascent, descent, gap) = font_extents(font, font_size);
                if ascent + descent + gap > shaped.ascent + shaped.descent + shaped.gap {
                    (shaped.ascent, shaped.descent, shaped.gap) = (ascent, descent, gap);
                }
                if let Some(shaper) = &shaper {
                    let mut run = Run::default();
                    shaper.shape(slice, font, !rtl, f32::MAX, &mut run);
                    shaped.advances.extend(
                        run.advances
                            .into_iter()
                            .map(|(offset, width)| (start + offset, width)),
                    );
                    continue;
                }
            }
            shaped.advances.extend(
                slice
                    .char_indices()
                    .filter(|(_, ch)| !ch.is_control())
                    .map(|(offset, _)| (start + offset, font_size * 0.5)),
            );
        }
        shaped.advances.sort_by_key(|&(offset, _)| offset);
        let shaped = Arc::new(shaped);
        if self.runs.len() == 512 {
            if let Some(old) = self.order.pop_front() {
                self.runs.remove(&old);
            }
        }
        self.order.push_back(key.clone());
        self.runs.insert(key, Arc::clone(&shaped));
        shaped
    }

    pub fn measure(
        &mut self,
        value: &str,
        style: &Properties,
        faces: &FontSet,
        root: f32,
    ) -> Measured {
        let size = font_size(style, root);
        let weight = match get(style, "font-weight") {
            "bold" | "bolder" => 700.0,
            value => number(value).unwrap_or(400.0),
        };
        let whitespace = get(style, "white-space");
        let preserve = matches!(whitespace, "pre" | "pre-wrap" | "break-spaces");
        let explicit_newlines = preserve || whitespace == "pre-line";
        let normal = |value: &str| {
            let mut result = String::new();
            let mut space = false;
            for ch in value.chars() {
                if ch.is_ascii_whitespace() {
                    if !space {
                        result.push(' ');
                    }
                    space = true;
                } else {
                    result.push(ch);
                    space = false;
                }
            }
            result
        };
        let text = if preserve {
            value.replace("\r\n", "\n").replace('\r', "\n")
        } else if explicit_newlines {
            value.split('\n').map(normal).collect::<Vec<_>>().join("\n")
        } else {
            normal(value)
        };
        let shaped = self.shape(&text, size, faces, get(style, "direction") == "rtl", weight);
        let line = get(style, "line-height");
        let height = number(line)
            .map(|scale| scale * size)
            .or_else(|| pixels(line, size, root))
            .or_else(|| {
                line.strip_suffix('%')
                    .and_then(number)
                    .map(|value| value / 100.0 * size)
            })
            .filter(|value| *value > 0.0)
            .unwrap_or(shaped.ascent + shaped.descent + shaped.gap);
        let baseline = shaped.ascent + (height - shaped.ascent - shaped.descent - shaped.gap) / 2.0;
        let letter = pixels(get(style, "letter-spacing"), size, root).unwrap_or(0.0);
        let word = pixels(get(style, "word-spacing"), size, root).unwrap_or(0.0);
        let wraps = !matches!(whitespace, "nowrap" | "pre");
        let break_graphemes = wraps
            && (get(style, "word-break") == "break-all"
                || get(style, "overflow-wrap") == "anywhere");
        let break_long_words =
            wraps && !break_graphemes && get(style, "overflow-wrap") == "break-word";
        let mut breaks: Vec<_> = linebreaks(&text)
            .filter_map(|(end, opportunity)| {
                let forced =
                    opportunity == BreakOpportunity::Mandatory && text[..end].ends_with('\n');
                (wraps || forced || end == text.len()).then_some((end, forced))
            })
            .collect();
        if break_graphemes {
            breaks.extend(
                text.grapheme_indices(true)
                    .skip(1)
                    .map(|(offset, _)| (offset, false)),
            );
            breaks.sort_unstable_by_key(|&(end, forced)| (end, !forced));
            breaks.dedup_by_key(|point| point.0);
        }
        let mut shaped_cursor = shaped.advances.iter().peekable();
        let mut start = 0;
        let parts = breaks
            .into_iter()
            .filter(|(end, _)| *end > 0)
            .map(|(end, forced)| {
                let segment = &text[start..end];
                let trimmed_end = if preserve {
                    end
                } else {
                    start + segment.trim_end_matches(' ').len()
                };
                let width_until =
                    |cursor: &mut std::iter::Peekable<std::slice::Iter<'_, (usize, f32)>>,
                     boundary| {
                        let mut width = 0.0;
                        while cursor
                            .peek()
                            .is_some_and(|&&(offset, _): &&(usize, f32)| offset < boundary)
                        {
                            width += cursor.next().unwrap().1;
                        }
                        width
                    };
                let count =
                    |value: &str| value.chars().filter(|ch| !ch.is_control()).count() as f32;
                let content = &text[start..trimmed_end];
                let emergency_breaks: Vec<_> = if break_long_words {
                    content
                        .grapheme_indices(true)
                        .map(|(offset, grapheme)| {
                            width_until(&mut shaped_cursor, start + offset + grapheme.len())
                                + letter * count(grapheme)
                        })
                        .collect()
                } else {
                    Vec::new()
                };
                let full = if break_long_words {
                    emergency_breaks.iter().sum()
                } else {
                    width_until(&mut shaped_cursor, trimmed_end) + letter * count(content)
                };
                let trimmed = width_until(&mut shaped_cursor, end)
                    + (letter + word) * count(&text[trimmed_end..end]);
                start = end;
                LinePart {
                    width: full + trimmed,
                    trailing: trimmed,
                    forced,
                    emergency_breaks,
                }
            })
            .collect();
        Measured {
            parts,
            height,
            baseline,
        }
    }

    pub fn control(&mut self, style: &Properties, faces: &FontSet, root: f32) -> Option<Size<f32>> {
        let size = font_size(style, root);
        let kind = get(style, "-control-type");
        let value = get(style, "-control-value");
        let height = self.measure(" ", style, faces, root).height;
        let width = |this: &mut Self, text: &str| {
            let shape = this.shape(
                text,
                size,
                faces,
                false,
                number(get(style, "font-weight")).unwrap_or(400.0),
            );
            shape
                .advances
                .iter()
                .map(|(_, advance)| advance)
                .sum::<f32>()
        };
        let (width, height) = match kind {
            "button" | "submit" | "reset" => {
                let measured = self.measure(value, style, faces, root);
                let run = measured.layout(AvailableSpace::MaxContent);
                (run.size.width, height.max(measured.height))
            }
            "file" => {
                let button = get(style, "-control-button-label");
                let empty = get(style, "-control-empty-label");
                let widest =
                    (34.0 * width(self, "0")).max(width(self, button) + 20.0 + width(self, empty));
                (
                    widest.ceil(),
                    self.measure(button, style, faces, root).height + 6.0,
                )
            }
            "text" | "search" | "password" | "tel" | "email" | "url" => {
                let face = Self::typeface(faces.first()?)?;
                let head = face.copy_table_data(tag(b"head"))?;
                let range = signed(head.as_bytes(), 40)? - signed(head.as_bytes(), 36)?;
                let average = width(self, "x");
                let average = average.max(average.round());
                let maximum =
                    (range.max(0.0) * size / face.units_per_em().unwrap_or(1000) as f32).round();
                let cols = number(get(style, "-control-size")).unwrap_or(20.0).max(1.0);
                (
                    (average * cols + (maximum - average).max(0.0)).ceil(),
                    height,
                )
            }
            "time" => {
                let time_size = pixels(get(style, "font-size"), 16.0, root)
                    .unwrap_or(13.3333)
                    .clamp(0.0, 4096.0);
                let time_width = |this: &mut Self, text: &str| {
                    let shape = this.shape(
                        text,
                        time_size,
                        faces,
                        false,
                        number(get(style, "font-weight")).unwrap_or(400.0),
                    );
                    shape
                        .advances
                        .iter()
                        .map(|(_, advance)| advance)
                        .sum::<f32>()
                };
                let minimum = get(style, "-control-min");
                let part =
                    |text: &str, index| text.split(':').nth(index).and_then(number).unwrap_or(0.0);
                let step = number(get(style, "-control-step"))
                    .filter(|value| *value > 0.0)
                    .unwrap_or(60.0);
                let seconds_value = part(value, 2);
                let seconds_min = part(minimum, 2);
                let millis = seconds_value.fract() != 0.0
                    || seconds_min.fract() != 0.0
                    || step.fract() != 0.0;
                let seconds =
                    millis || seconds_value != 0.0 || seconds_min != 0.0 || step % 60.0 != 0.0;
                let mut digit = time_width(self, "0");
                let mut colon = time_width(self, ":");
                if cfg!(target_os = "windows")
                    && faces
                        .first()
                        .and_then(Self::typeface)
                        .is_some_and(|face| bitmap_strike(&face, time_size))
                {
                    digit = digit.round();
                    colon = colon.round();
                }
                let fields = if seconds { 3.0 } else { 2.0 };
                let edit = (2.0 * digit + 2.0) * fields
                    + colon * (fields - 1.0)
                    + if millis {
                        3.0 * digit + 2.0 + colon
                    } else {
                        0.0
                    };
                let picker = (time_size * 1.05).ceil() + 6.0;
                (edit.ceil() + 8.0 + picker, (height + 2.0).max(picker))
            }
            _ => return None,
        };
        Some(Size { width, height })
    }
}

fn font_size(style: &Properties, root: f32) -> f32 {
    let value = get(style, "font-size");
    value
        .strip_suffix("px")
        .and_then(number)
        .or_else(|| pixels(value, 16.0, root))
        .unwrap_or(16.0)
        .clamp(0.0, 4096.0)
}

fn tag(bytes: &[u8; 4]) -> u32 {
    u32::from_be_bytes(*bytes)
}
fn signed(table: &[u8], off: usize) -> Option<f32> {
    Some(i16::from_be_bytes(table.get(off..off + 2)?.try_into().ok()?) as f32)
}
fn unsigned(table: &[u8], off: usize) -> Option<u16> {
    Some(u16::from_be_bytes(
        table.get(off..off + 2)?.try_into().ok()?,
    ))
}

fn font_extents(font: &Font, size: f32) -> (f32, f32, f32) {
    let face = font.typeface();
    let scale = size / face.units_per_em().unwrap_or(1000) as f32;
    let os2 = face.copy_table_data(tag(b"OS/2"));
    let hhea = face.copy_table_data(tag(b"hhea"));
    let mut metrics = None;
    if let Some(os2) = &os2 {
        let table = os2.as_bytes();
        metrics = if unsigned(table, 62).is_some_and(|selection| selection & 128 != 0) {
            signed(table, 68).zip(signed(table, 70).map(|desc| -desc))
        } else {
            unsigned(table, 74)
                .zip(unsigned(table, 76))
                .map(|(a, d)| (a as f32, d as f32))
        };
    }
    if let Some(hhea) = hhea {
        let table = hhea.as_bytes();
        if let Some((ascent, desc)) = signed(table, 4).zip(signed(table, 6)) {
            let (a, d) = metrics.unwrap_or((ascent, -desc));
            let gap = (ascent - desc + signed(table, 8).unwrap_or(0.0) - a - d).max(0.0);
            return (
                (a.max(0.0) * scale).round(),
                (d.max(0.0) * scale).round(),
                (gap * scale).round(),
            );
        }
    }
    let (_, metrics) = font.metrics();
    (
        (-metrics.ascent).round(),
        metrics.descent.round(),
        metrics.leading.max(0.0).round(),
    )
}

fn bitmap_strike(face: &Typeface, size: f32) -> bool {
    let Some(data) = face.copy_table_data(tag(b"EBLC")) else {
        return false;
    };
    let bytes = data.as_bytes();
    let glyphs: Vec<_> = "0123456789:."
        .chars()
        .map(|ch| face.unichar_to_glyph(ch as i32))
        .collect();
    let ppem = size.round() as u8;
    bytes
        .get(8..)
        .unwrap_or_default()
        .chunks_exact(48)
        .any(|strike| {
            let first = unsigned(strike, 40).unwrap();
            let last = unsigned(strike, 42).unwrap();
            strike[45] == ppem
                && glyphs
                    .iter()
                    .all(|&glyph| glyph != 0 && (first..=last).contains(&glyph))
        })
}
