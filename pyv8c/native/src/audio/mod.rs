// Offline waveform generator using Chromium's packed real inverse FFT layout.
// The packing algorithm follows rustfft_ffi.rs, Copyright 2026 The Chromium
// Authors, under the BSD 3-Clause license in third_party_licenses/CHROMIUM-LICENSE.txt.
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use rustfft::{FftPlanner, num_complex::Complex};
use std::sync::OnceLock;
mod compressor;

const SIZE: usize = 4096;
const HALF: usize = SIZE / 2;

fn waveform_table(kind: usize, limit: usize) -> Vec<f32> {
    let mut imag = vec![0.0f32; HALF];
    for n in 1..=limit.min(HALF - 1) {
        let p = 2.0f32 / ((n as f32) * std::f32::consts::PI);
        let b = match kind {
            0 => {
                if n == 1 {
                    1.0
                } else {
                    0.0
                }
            }
            1 => {
                if n % 2 != 0 {
                    2.0 * p
                } else {
                    0.0
                }
            }
            2 => {
                if n % 2 != 0 {
                    p
                } else {
                    -p
                }
            }
            3 => {
                if n % 2 != 0 {
                    2.0 * (p * p) * if ((n - 1) / 2) % 2 == 0 { 1.0 } else { -1.0 }
                } else {
                    0.0
                }
            }
            _ => unreachable!(),
        };
        imag[n] = -b * SIZE as f32;
    }
    let mut z = vec![Complex::<f32>::new(0.0, 0.0); HALF];
    for k in 1..=(HALF - 1) / 2 {
        let x = Complex::new(0.0f32, imag[k]);
        let y = Complex::new(0.0f32, -imag[HALF - k]);
        let even = (x + y) * 0.5f32;
        let theta = -2.0 * std::f64::consts::PI * k as f64 / SIZE as f64;
        let twiddle = Complex::new((0.5 * theta.cos()) as f32, (0.5 * theta.sin()) as f32);
        let odd = twiddle.conj() * (x - y);
        let iodd = Complex::new(-odd.im, odd.re);
        z[k] = even + iodd;
        z[HALF - k] = (even - iodd).conj();
    }
    z[HALF / 2] = Complex::new(0.0, -imag[HALF / 2]);
    FftPlanner::<f32>::new()
        .plan_fft_inverse(HALF)
        .process(&mut z);
    let mut out = Vec::with_capacity(SIZE);
    for value in z {
        out.push(value.re / HALF as f32);
        out.push(value.im / HALF as f32);
    }
    out
}

fn generate_wave_tables() -> Vec<u8> {
    let mut out = Vec::with_capacity(4 * 36 * SIZE * 4);
    for kind in 0..4 {
        let full = waveform_table(kind, HALF - 1);
        let peak = full.iter().fold(0.0f32, |a, &x| a.max(x.abs()));
        let scale = 1.0f32 / peak;
        for index in 0..36 {
            let exponent = -(index as f32 * 400.0f32) / 1200.0f32;
            let limit = ((2.0f64.powf(exponent as f64) as f32) * HALF as f32) as usize;
            for x in waveform_table(kind, limit) {
                out.extend_from_slice(&(x * scale).to_le_bytes());
            }
        }
    }
    out
}

static WAVE_TABLES: OnceLock<Vec<u8>> = OnceLock::new();

fn wave_sample(kind: usize, position: f64, high: usize, low: usize, mix: f32) -> f32 {
    let tables = WAVE_TABLES.get_or_init(generate_wave_tables);
    let index = position as usize;
    let following = (index + 1) % SIZE;
    let fraction = position as f32 - index as f32;
    let sample = |band: usize| {
        let get = |i: usize| {
            let pos = ((kind * 36 + band) * SIZE + i) * 4;
            f32::from_le_bytes(tables[pos..pos + 4].try_into().unwrap())
        };
        let first = get(index);
        first + fraction * (get(following) - first)
    };
    let upper = sample(high);
    upper + mix * (sample(low) - upper)
}

#[derive(Default)]
struct OscillatorState {
    phase: f64,
    lanes: Option<[f32; 4]>,
    lane: usize,
    increment: Option<f32>,
}

impl OscillatorState {
    fn step(&mut self, frame: usize, increment: f32, vector: bool) -> f64 {
        let read_phase = if vector {
            if frame % 128 == 0 || self.lanes.is_none() || self.increment != Some(increment) {
                let start = self.phase * SIZE as f64;
                let mut lanes = [0.0; 4];
                for (lane, value) in lanes.iter_mut().enumerate() {
                    *value = ((start + lane as f64 * increment as f64) as f32) % SIZE as f32;
                }
                self.lanes = Some(lanes);
                self.increment = Some(increment);
                self.lane = 0;
            }
            let lanes = self.lanes.as_mut().unwrap();
            let position = lanes[self.lane];
            lanes[self.lane] = (position + (4.0f64 * increment as f64) as f32) % SIZE as f32;
            self.lane = (self.lane + 1) % 4;
            position as f64 / SIZE as f64
        } else {
            self.lanes = None;
            self.phase
        };
        self.phase = (self.phase + increment as f64 / SIZE as f64).rem_euclid(1.0);
        read_phase
    }
}

#[pyfunction]
fn oscillator_phase_step(
    phase: f64,
    lanes: Option<[f32; 4]>,
    lane: usize,
    previous_increment: Option<f32>,
    increment: f32,
    frame: usize,
    vector: bool,
) -> PyResult<(f64, f64, Option<[f32; 4]>, usize, Option<f32>)> {
    if lane >= 4 || !phase.is_finite() || !increment.is_finite() {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "invalid oscillator state",
        ));
    }
    let mut state = OscillatorState {
        phase,
        lanes,
        lane,
        increment: previous_increment,
    };
    let read_phase = state.step(frame, increment, vector);
    Ok((
        read_phase,
        state.phase,
        state.lanes,
        state.lane,
        state.increment,
    ))
}

#[pyfunction]
fn oscillator_waveform(
    kind: usize,
    phase: f64,
    high: usize,
    low: usize,
    mix: f32,
) -> PyResult<f32> {
    if kind >= 4 || high >= 36 || low >= 36 || !(0.0..1.0).contains(&phase) {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "invalid waveform parameters",
        ));
    }
    Ok(wave_sample(kind, phase * SIZE as f64, high, low, mix))
}

/// Render a constant x86 oscillator with the same state and interpolation code.
#[pyfunction]
fn oscillator_block<'py>(
    py: Python<'py>,
    kind: usize,
    count: usize,
    increment: f32,
    high: usize,
    low: usize,
    mix: f32,
) -> PyResult<(Bound<'py, PyBytes>, f64, Option<Vec<f32>>, usize)> {
    use pyo3::exceptions::PyValueError;
    if kind >= 4 || high >= 36 || low >= 36 || !increment.is_finite() || increment <= 0.0 {
        return Err(PyValueError::new_err("invalid oscillator parameters"));
    }
    let render = || {
        let mut out = Vec::with_capacity(count.saturating_mul(4));
        let mut state = OscillatorState::default();
        for frame in 0..count {
            let phase = state.step(frame, increment, frame >= 128);
            out.extend_from_slice(
                &wave_sample(kind, phase * SIZE as f64, high, low, mix).to_le_bytes(),
            );
        }
        (out, state)
    };
    let (out, state) = if count >= 4096 {
        py.detach(render)
    } else {
        render()
    };
    Ok((
        PyBytes::new(py, &out),
        state.phase,
        state.lanes.map(|lanes| lanes.to_vec()),
        state.lane,
    ))
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<compressor::CompressorDSP>()?;
    m.add_function(wrap_pyfunction!(oscillator_phase_step, m)?)?;
    m.add_function(wrap_pyfunction!(oscillator_waveform, m)?)?;
    m.add_function(wrap_pyfunction!(oscillator_block, m)?)?;
    Ok(())
}
