use pyo3::{exceptions::PyValueError, prelude::*, types::PyBytes};
use skia_safe::{
    AlphaType, BlendMode, Color, Color4f, ColorType, Data, FilterMode, Font, FontMgr, Image,
    ImageInfo, Matrix, Paint, PathBuilder, PathFillType, Point, Rect, SamplingOptions, Surface,
    TileMode, canvas::SrcRectConstraint, gradient, images, jpeg_encoder, paint, png_encoder,
    surfaces, webp_encoder,
};
use skia_safe::{ConditionallySend, Sendable};
use std::{collections::HashMap, sync::Mutex};

type Rgba = (u8, u8, u8, u8);
type Bounds = (f32, f32, f32, f32);
type Transform = (f32, f32, f32, f32, f32, f32);
type Gradient = (Vec<f32>, Vec<(f32, Rgba)>, Transform);
type Stroke = (String, String, Vec<f32>);
type Face = (String, u32);
type GlyphRun = (Face, Vec<u16>, Vec<(f32, f32)>);

fn color((r, g, b, a): Rgba) -> Color {
    Color::from_argb(a, r, g, b)
}

fn rect((l, t, r, b): Bounds) -> Rect {
    Rect::new(l, t, r, b)
}

fn matrix((a, b, c, d, e, f): Transform) -> Matrix {
    Matrix::new_all(a, c, e, b, d, f, 0.0, 0.0, 1.0)
}

fn rgba_info(width: i32, height: i32) -> ImageInfo {
    ImageInfo::new(
        (width, height),
        ColorType::RGBA8888,
        AlphaType::Unpremul,
        None,
    )
}

fn invalid_size() -> PyErr {
    PyValueError::new_err("invalid raster dimensions or RGBA length")
}

fn row_bytes(width: i32, height: i32) -> PyResult<(usize, usize)> {
    let stride = usize::try_from(width).ok().and_then(|w| w.checked_mul(4));
    stride
        .and_then(|stride| {
            let length = stride.checked_mul(usize::try_from(height).ok()?)?;
            (length <= isize::MAX as usize).then_some((stride, length))
        })
        .ok_or_else(invalid_size)
}

fn make_surface(width: i32, height: i32) -> PyResult<Surface> {
    surfaces::raster_n32_premul((width, height)).ok_or_else(invalid_size)
}

fn blend_mode(mode: &str) -> BlendMode {
    match mode {
        "clear" => BlendMode::Clear,
        "copy" => BlendMode::Src,
        "source-in" => BlendMode::SrcIn,
        "source-out" => BlendMode::SrcOut,
        "source-atop" => BlendMode::SrcATop,
        "destination-over" => BlendMode::DstOver,
        "destination-in" => BlendMode::DstIn,
        "destination-out" => BlendMode::DstOut,
        "destination-atop" => BlendMode::DstATop,
        "xor" => BlendMode::Xor,
        "lighter" => BlendMode::Plus,
        "multiply" => BlendMode::Multiply,
        "screen" => BlendMode::Screen,
        "overlay" => BlendMode::Overlay,
        "darken" => BlendMode::Darken,
        "lighten" => BlendMode::Lighten,
        "color-dodge" => BlendMode::ColorDodge,
        "color-burn" => BlendMode::ColorBurn,
        "hard-light" => BlendMode::HardLight,
        "soft-light" => BlendMode::SoftLight,
        "difference" => BlendMode::Difference,
        "exclusion" => BlendMode::Exclusion,
        "hue" => BlendMode::Hue,
        "saturation" => BlendMode::Saturation,
        "color" => BlendMode::Color,
        "luminosity" => BlendMode::Luminosity,
        _ => BlendMode::SrcOver,
    }
}

fn brush(rgba: Rgba, gradient: Option<Gradient>, blend: &str, stroke: Option<Stroke>) -> Paint {
    let mut paint = Paint::default();
    paint
        .set_color(color(rgba))
        .set_anti_alias(true)
        .set_blend_mode(blend_mode(blend));
    if let Some((coords, mut stops, transform)) = gradient {
        if stops.len() == 1 {
            stops.push((1.0, stops[0].1));
        }
        let (positions, colors): (Vec<_>, Vec<_>) = stops
            .into_iter()
            .map(|(offset, rgba)| (offset, Color4f::from(color(rgba))))
            .unzip();
        let transform = matrix(transform);
        let stops = gradient::Colors::new(&colors, Some(&positions), TileMode::Clamp, None);
        let spec = gradient::Gradient::new(
            stops,
            gradient::Interpolation {
                in_premul: gradient::interpolation::InPremul::Yes,
                ..Default::default()
            },
        );
        let shader = if colors.is_empty() {
            None
        } else {
            match coords.as_slice() {
                [x0, y0, x1, y1] if (x0, y0) != (x1, y1) => {
                    gradient::shaders::linear_gradient(((*x0, *y0), (*x1, *y1)), &spec, &transform)
                }
                [x0, y0, r0, x1, y1, r1] => gradient::shaders::two_point_conical_gradient(
                    ((*x0, *y0), *r0),
                    ((*x1, *y1), *r1),
                    &spec,
                    &transform,
                ),
                _ => None,
            }
        };
        if let Some(shader) = shader {
            paint.set_color(Color::WHITE).set_shader(shader);
        } else {
            paint.set_color(Color::TRANSPARENT);
        }
    }
    if let Some((cap, join, dash)) = stroke {
        paint.set_style(paint::Style::Stroke).set_stroke_width(1.0);
        paint.set_stroke_cap(match cap.as_str() {
            "round" => paint::Cap::Round,
            "square" => paint::Cap::Square,
            _ => paint::Cap::Butt,
        });
        paint.set_stroke_join(match join.as_str() {
            "round" => paint::Join::Round,
            "bevel" => paint::Join::Bevel,
            _ => paint::Join::Miter,
        });
        if !dash.is_empty() {
            paint.set_path_effect(skia_safe::dash_path_effect::new(&dash, 0.0));
        }
    }
    paint
}

fn drawing(
    canvas: &skia_safe::Canvas,
    transform: Transform,
    draw: impl FnOnce(&skia_safe::Canvas),
) {
    let count = canvas.save();
    canvas.concat(&matrix(transform));
    draw(canvas);
    canvas.restore_to_count(count);
}

#[pyclass(module = "pyv8c._native._native")]
pub struct RasterImage(Image);

#[pymethods]
impl RasterImage {
    #[staticmethod]
    fn decode(encoded: &[u8]) -> Option<Self> {
        images::deferred_from_encoded_data(Data::new_copy(encoded), None).map(Self)
    }

    #[staticmethod]
    fn from_rgba(width: i32, height: i32, pixels: &[u8]) -> PyResult<Self> {
        let (stride, length) = row_bytes(width, height)?;
        if length != pixels.len() {
            return Err(invalid_size());
        }
        images::raster_from_data(&rgba_info(width, height), Data::new_copy(pixels), stride)
            .map(Self)
            .ok_or_else(invalid_size)
    }

    #[getter]
    fn size(&self) -> (i32, i32) {
        (self.0.width(), self.0.height())
    }

    fn read<'py>(
        &self,
        py: Python<'py>,
        x: i32,
        y: i32,
        width: i32,
        height: i32,
    ) -> PyResult<Bound<'py, PyBytes>> {
        read_rgba(py, width, height, |info, pixels, stride| {
            self.0.read_pixels(
                info,
                pixels,
                stride,
                (x, y),
                skia_safe::image::CachingHint::Allow,
            );
        })
    }

    fn crop(&self, bounds: (i32, i32, i32, i32)) -> PyResult<Self> {
        let (x, y, width, height) = bounds;
        let mut surface = make_surface(width, height)?;
        surface
            .canvas()
            .draw_image(&self.0, (-(x as f32), -(y as f32)), None);
        Ok(Self(surface.image_snapshot()))
    }

    fn resize(&self, py: Python<'_>, size: (i32, i32)) -> PyResult<Self> {
        let image = &self.0;
        let resize = || {
            let mut surface = surfaces::raster_n32_premul(size)?;
            surface.canvas().draw_image_rect_with_sampling_options(
                image,
                None,
                Rect::from_size((size.0 as f32, size.1 as f32)),
                SamplingOptions::from(FilterMode::Linear),
                &Paint::default(),
            );
            Some(Self(surface.image_snapshot()))
        };
        // Keep short resizes attached. A deferred image can also decode at this draw.
        let source_pixels = i64::from(image.width()) * i64::from(image.height());
        let target_pixels = i64::from(size.0.max(0)) * i64::from(size.1.max(0));
        if source_pixels.max(target_pixels) >= 512 * 512 {
            py.detach(resize)
        } else {
            resize()
        }
        .ok_or_else(invalid_size)
    }

    fn encode<'py>(
        &self,
        py: Python<'py>,
        kind: &str,
        quality: Option<f32>,
    ) -> PyResult<Bound<'py, PyBytes>> {
        let image = &self.0;
        let data = match kind {
            "image/png" => {
                let mut options = png_encoder::Options::default();
                options.filter_flags = png_encoder::FilterFlag::UP;
                options.z_lib_level = 1;
                py.detach(|| png_encoder::encode_image(None, image, &options))
            }
            "image/jpeg" => {
                let options = jpeg_encoder::Options {
                    quality: (quality.unwrap_or(0.92) * 100.0).round() as u32,
                    alpha_option: jpeg_encoder::AlphaOption::BlendOnBlack,
                    downsample: if quality == Some(1.0) {
                        jpeg_encoder::Downsample::No
                    } else {
                        jpeg_encoder::Downsample::BothDirections
                    },
                    ..Default::default()
                };
                py.detach(|| jpeg_encoder::encode_image(None, image, &options))
            }
            "image/webp" => {
                let options = webp_encoder::Options {
                    compression: if quality == Some(1.0) {
                        webp_encoder::Compression::Lossless
                    } else {
                        webp_encoder::Compression::Lossy
                    },
                    quality: if quality == Some(1.0) {
                        75.0
                    } else {
                        quality.unwrap_or(0.8) * 100.0
                    },
                };
                py.detach(|| webp_encoder::encode_image(None, image, &options))
            }
            _ => None,
        }
        .ok_or_else(|| PyValueError::new_err("Skia image encoding failed"))?;
        Ok(PyBytes::new(py, data.as_bytes()))
    }
}

fn read_rgba<'py>(
    py: Python<'py>,
    width: i32,
    height: i32,
    read: impl FnOnce(&ImageInfo, &mut [u8], usize),
) -> PyResult<Bound<'py, PyBytes>> {
    let (stride, length) = row_bytes(width, height)?;
    PyBytes::new_with(py, length, |pixels| {
        pixels.fill(0);
        if length != 0 {
            read(&rgba_info(width, height), pixels, stride);
        }
        Ok(())
    })
}

#[pyclass(module = "pyv8c._native._native")]
pub struct RasterCanvas {
    surface: Mutex<Option<Sendable<Surface>>>,
    fonts: HashMap<(Face, u32), Font>,
}

fn send_raster_surface(surface: Surface) -> PyResult<Sendable<Surface>> {
    surface
        .wrap_send()
        .map_err(|_| PyValueError::new_err("Skia raster surface is shared"))
}

impl RasterCanvas {
    fn with_surface<T>(&mut self, draw: impl FnOnce(&mut Surface) -> T) -> PyResult<T> {
        // Only CPU raster surfaces enter this slot. No operation exposes or clones the
        // Surface handle. A snapshot shares pixels with copy-on-write, not the handle.
        // PyO3's exclusive borrow serializes access; the mutex makes storage Sync.
        let slot = self
            .surface
            .get_mut()
            .expect("surface mutex is never locked");
        let mut surface = slot
            .take()
            .ok_or_else(|| PyValueError::new_err("Skia raster surface is unavailable"))?
            .into_inner();
        let result = draw(&mut surface);
        *slot = Some(send_raster_surface(surface)?);
        Ok(result)
    }

    fn font(&mut self, face: Face, size: f32) -> &Font {
        self.fonts
            .entry((face, size.to_bits()))
            .or_insert_with_key(|((path, index), _)| {
                let manager = FontMgr::new();
                let typeface = std::fs::read(path)
                    .ok()
                    .and_then(|data| manager.new_from_data(Data::new_copy(&data), *index))
                    .or_else(|| manager.legacy_make_typeface(None, skia_safe::FontStyle::normal()));
                let mut font = typeface.map_or_else(Font::default, |face| Font::new(face, size));
                font.set_size(size).set_subpixel(true);
                font
            })
    }
}

#[pymethods]
impl RasterCanvas {
    #[new]
    fn new(width: i32, height: i32) -> PyResult<Self> {
        Ok(Self {
            surface: Mutex::new(Some(send_raster_surface(make_surface(width, height)?)?)),
            fonts: HashMap::new(),
        })
    }

    fn snapshot(&mut self) -> PyResult<RasterImage> {
        self.with_surface(|surface| RasterImage(surface.image_snapshot()))
    }

    fn draw_rect(
        &mut self,
        bounds: Bounds,
        transform: Transform,
        rgba: Rgba,
        gradient: Option<Gradient>,
        blend: &str,
        stroke: Option<Stroke>,
    ) -> PyResult<()> {
        let paint = brush(rgba, gradient, blend, stroke);
        self.with_surface(|surface| {
            drawing(surface.canvas(), transform, |canvas| {
                canvas.draw_rect(rect(bounds), &paint);
            })
        })
    }

    fn draw_path(
        &mut self,
        parts: Vec<Vec<(f32, f32)>>,
        rgba: Rgba,
        gradient: Option<Gradient>,
        blend: &str,
        stroke: Option<Stroke>,
        even_odd: bool,
    ) -> PyResult<()> {
        let mut path = PathBuilder::new();
        if even_odd {
            path.set_fill_type(PathFillType::EvenOdd);
        }
        for part in parts {
            let closed = part.len() > 1 && part.first() == part.last();
            let points: Vec<_> = part.into_iter().map(Point::from).collect();
            path.add_polygon(&points, closed);
        }
        self.with_surface(|surface| {
            surface
                .canvas()
                .draw_path(&path.detach(), &brush(rgba, gradient, blend, stroke));
        })
    }

    #[allow(clippy::too_many_arguments)]
    fn draw_text(
        &mut self,
        text: &str,
        point: (f32, f32),
        transform: Transform,
        face: (String, u32, f32),
        anchors: (char, char),
        rgba: Rgba,
        gradient: Option<Gradient>,
        blend: &str,
        stroke: Option<Stroke>,
        shaped: Option<(f32, Vec<GlyphRun>)>,
    ) -> PyResult<()> {
        let (path, index, size) = face;
        let font = self.font((path, index), size).clone();
        let paint = brush(rgba, gradient, blend, stroke);
        let metrics = font.metrics().1;
        let width = shaped
            .as_ref()
            .map_or_else(|| font.measure_str(text, None).0, |run| run.0);
        let x = point.0
            - match anchors.0 {
                'm' => width * 0.5,
                'r' => width,
                _ => 0.0,
            };
        let y = point.1
            - match anchors.1 {
                'a' => metrics.ascent,
                'm' => (metrics.ascent + metrics.descent) * 0.5,
                'd' => metrics.descent,
                _ => 0.0,
            };
        if let Some((_, runs)) = shaped {
            let mut builder = skia_safe::TextBlobBuilder::new();
            for (face, glyphs, positions) in runs {
                if glyphs.is_empty() || glyphs.len() != positions.len() {
                    continue;
                }
                let (dst_glyphs, dst_positions) =
                    builder.alloc_run_pos(self.font(face, size), glyphs.len(), None);
                dst_glyphs.copy_from_slice(&glyphs);
                for (output, point) in dst_positions.iter_mut().zip(positions) {
                    *output = Point::from(point);
                }
            }
            if let Some(blob) = builder.make() {
                self.with_surface(|surface| {
                    drawing(surface.canvas(), transform, |canvas| {
                        canvas.draw_text_blob(blob, (x, y), &paint);
                    })
                })?;
            }
        } else {
            self.with_surface(|surface| {
                drawing(surface.canvas(), transform, |canvas| {
                    canvas.draw_str(text, (x, y), &font, &paint);
                })
            })?;
        }
        Ok(())
    }

    fn draw_image(
        &mut self,
        image: &RasterImage,
        source: Bounds,
        destination: Bounds,
        transform: Transform,
        blend: &str,
    ) -> PyResult<()> {
        let mut paint = Paint::default();
        paint.set_blend_mode(blend_mode(blend));
        self.with_surface(|surface| {
            drawing(surface.canvas(), transform, |canvas| {
                canvas.draw_image_rect_with_sampling_options(
                    &image.0,
                    Some((&rect(source), SrcRectConstraint::Strict)),
                    rect(destination),
                    SamplingOptions::from(FilterMode::Linear),
                    &paint,
                );
            })
        })
    }

    fn read<'py>(
        &mut self,
        py: Python<'py>,
        x: i32,
        y: i32,
        width: i32,
        height: i32,
    ) -> PyResult<Bound<'py, PyBytes>> {
        self.with_surface(|surface| {
            read_rgba(py, width, height, |info, pixels, stride| {
                surface.read_pixels(info, pixels, stride, (x, y));
            })
        })?
    }

    fn write(
        &mut self,
        offset: (i32, i32),
        size: (i32, i32),
        pixels: &[u8],
        crop: (i32, i32, i32, i32),
    ) -> PyResult<()> {
        let (stride, length) = row_bytes(size.0, size.1)?;
        let (left, top, right, bottom) = crop;
        if length != pixels.len()
            || left < 0
            || top < 0
            || right > size.0
            || bottom > size.1
            || right <= left
            || bottom <= top
        {
            return Err(invalid_size());
        }
        let start = top as usize * stride + left as usize * 4;
        let written = self.with_surface(|surface| {
            surface.canvas().write_pixels(
                &rgba_info(right - left, bottom - top),
                &pixels[start..],
                stride,
                offset,
            )
        })?;
        if written {
            Ok(())
        } else {
            Err(PyValueError::new_err(
                "Skia could not write the canvas pixels",
            ))
        }
    }
}
