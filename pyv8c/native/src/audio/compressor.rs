// Adapted from Blink's dynamics compressor.
/*
Copyright (C) 2011 Google Inc. All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:
1. Redistributions of source code must retain the above copyright notice,
   this list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.
3. Neither the name of Apple Computer, Inc. ("Apple") nor the names of its
   contributors may be used to endorse or promote products derived from this
   software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY APPLE AND ITS CONTRIBUTORS "AS IS" AND ANY
EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL APPLE OR ITS CONTRIBUTORS BE LIABLE FOR ANY
DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF
THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
*/
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::collections::VecDeque;

type Params = (f64, f64, f64, f64, f64);

fn f(x: f64) -> f64 {
    x as f32 as f64
}
fn add(a: f64, b: f64) -> f64 {
    f(a + b)
}
fn sub(a: f64, b: f64) -> f64 {
    f(a - b)
}
fn mul(a: f64, b: f64) -> f64 {
    f(a * b)
}
fn div(a: f64, b: f64) -> f64 {
    f(a / b)
}
fn db(x: f64) -> f64 {
    if x > 0.0 {
        mul(20.0, f(x.log10()))
    } else {
        f64::NEG_INFINITY
    }
}
fn linear(x: f64) -> f64 {
    f(10.0f64.powf(mul(f(0.05), x)))
}
fn polynomial(terms: &[(f64, f64)]) -> f64 {
    let mut result = mul(f(terms[0].0), terms[0].1);
    for &(coefficient, zone) in &terms[1..] {
        result = add(result, mul(f(coefficient), zone));
    }
    result
}
fn release_polynomial() -> [f64; 5] {
    let (a, b, c, d) = (f(0.09), f(0.16), f(0.42), f(0.98));
    [
        a,
        polynomial(&[
            (-1.5788320352845888, a),
            (2.3305837032074286, b),
            (-0.9141194204840429, c),
            (0.1623677525612032, d),
        ]),
        polynomial(&[
            (0.5334142869106424, a),
            (-1.272736789213631, b),
            (0.9258856042207512, c),
            (-0.18656310191776226, d),
        ]),
        polynomial(&[
            (0.08783463138207234, a),
            (-0.1694162967925622, b),
            (0.08588057951595272, c),
            (-0.00429891410546283, d),
        ]),
        polynomial(&[
            (-0.042416883008123074, a),
            (0.1115693827987602, b),
            (-0.09764676325265872, c),
            (0.028494263462021576, d),
        ]),
    ]
}

#[pyclass(module = "pyv8c._native.audio")]
pub struct CompressorDSP {
    sample_rate: f64,
    delay: VecDeque<f64>,
    detector: f64,
    gain: f64,
    #[pyo3(get)]
    reduction: f64,
    meter: f64,
    meter_rate: f64,
    max_attack: f64,
    #[pyo3(get)]
    frame: usize,
    parameters: Option<Params>,
    scaled_gain: f64,
    envelope_rate: f64,
    threshold: f64,
    slope: f64,
    knee_db: f64,
    knee_threshold: f64,
    knee_y_db: f64,
    k: f64,
    makeup: f64,
    release_polynomial: [f64; 5],
}

impl CompressorDSP {
    fn knee_curve(&self, value: f64, k: f64) -> f64 {
        if value < self.threshold {
            return value;
        }
        let exponent = mul(-k, sub(value, self.threshold));
        add(self.threshold, div(sub(1.0, f(exponent.exp())), k))
    }
    fn saturate(&self, value: f64) -> f64 {
        if value < self.knee_threshold {
            return self.knee_curve(value, self.k);
        }
        linear(add(
            self.knee_y_db,
            mul(self.slope, sub(db(value), self.knee_db)),
        ))
    }
    fn curve(&mut self, threshold: f64, knee: f64, ratio: f64) {
        self.threshold = linear(threshold);
        self.slope = div(1.0, ratio.max(1.0));
        self.knee_db = add(threshold, knee.max(0.0));
        self.knee_threshold = linear(self.knee_db);
        let (mut low, mut high, mut k) = (f(0.1), 10000.0, 5.0);
        let (x, x2) = (self.knee_threshold, f(self.knee_threshold * 1.001));
        let denominator = sub(db(x2), self.knee_db);
        for _ in 0..15 {
            let slope = div(
                sub(db(self.knee_curve(x2, k)), db(self.knee_curve(x, k))),
                denominator,
            );
            if slope < self.slope {
                high = k;
            } else {
                low = k;
            }
            k = f(mul(low, high).sqrt());
        }
        self.k = k;
        self.knee_y_db = db(self.knee_curve(self.knee_threshold, k));
        self.makeup = f(div(1.0, self.saturate(1.0)).powf(f(0.6)));
    }
    fn division(&mut self, parameters: Params) {
        // AudioParam automation is interpolated first; Blink then clamps each
        // computed value to the parameter's nominal range at the DSP boundary.
        let (threshold, knee, ratio, attack, release) = parameters;
        let parameters = (
            threshold.clamp(-100.0, 0.0),
            knee.clamp(0.0, 40.0),
            ratio.clamp(1.0, 20.0),
            attack.clamp(0.0, 1.0),
            release.clamp(0.0, 1.0),
        );
        let (threshold, knee, ratio, attack, release) = parameters;
        if self.parameters.map(|p| (p.0, p.1, p.2)) != Some((threshold, knee, ratio)) {
            self.curve(threshold, knee, ratio);
        }
        self.parameters = Some(parameters);
        let desired = self.detector.max(0.0).min(1.0);
        self.scaled_gain = div(f(desired.asin()), f(std::f64::consts::PI / 2.0));
        let releasing = self.scaled_gain > self.gain;
        let mut difference = if self.scaled_gain != 0.0 {
            db(div(self.gain, self.scaled_gain))
        } else if releasing {
            -1.0
        } else {
            1.0
        };
        if !difference.is_finite() {
            difference = if releasing { -1.0 } else { 1.0 };
        }
        if releasing {
            self.max_attack = -1.0;
            let x = mul(0.25, add(difference.max(-12.0).min(0.0), 12.0));
            let x2 = mul(x, x);
            let (x3, x4) = (mul(x2, x), mul(x2, x2));
            let release_frames = mul(self.sample_rate, release.max(0.0));
            let coeffs = self
                .release_polynomial
                .map(|coefficient| mul(release_frames, coefficient));
            let mut frames = coeffs[0];
            for (coefficient, power) in coeffs[1..].iter().zip([x, x2, x3, x4]) {
                frames = add(frames, mul(*coefficient, power));
            }
            self.envelope_rate = linear(div(5.0, frames.max(1.0)));
        } else {
            self.max_attack = self.max_attack.max(difference);
            let frames = mul(self.sample_rate, attack.max(f(0.001)));
            self.envelope_rate = sub(
                1.0,
                f(div(0.25, self.max_attack.max(0.5)).powf(div(1.0, frames))),
            );
        }
    }
    fn step(&mut self, input: f64, parameters: Params) -> f64 {
        if self.frame % 32 == 0 {
            self.division(parameters);
        }
        let value = f(input);
        let magnitude = value.abs();
        let attenuation = if magnitude > f(0.0001) {
            div(self.saturate(magnitude), magnitude)
        } else {
            1.0
        };
        let release_rate = sub(
            linear(div(
                2.0f64.max(-db(attenuation)),
                mul(f(0.0025), self.sample_rate),
            )),
            1.0,
        );
        let rate = if attenuation > self.detector {
            release_rate
        } else {
            1.0
        };
        self.detector = add(self.detector, mul(sub(attenuation, self.detector), rate)).min(1.0);
        if self.envelope_rate < 1.0 {
            self.gain = add(
                self.gain,
                mul(sub(self.scaled_gain, self.gain), self.envelope_rate),
            );
        } else {
            self.gain = mul(self.gain, self.envelope_rate).min(1.0);
        }
        let warped = f(mul(f(std::f64::consts::PI / 2.0), self.gain).sin());
        let meter = db(warped);
        self.meter = if meter < self.meter {
            meter
        } else {
            add(self.meter, mul(sub(meter, self.meter), f(self.meter_rate)))
        };
        self.reduction = self.meter;
        let delayed = if self.delay.is_empty() {
            value
        } else {
            self.delay.push_back(value);
            self.delay.pop_front().unwrap()
        };
        self.frame += 1;
        mul(delayed, mul(self.makeup, warped))
    }
}

#[pymethods]
impl CompressorDSP {
    #[new]
    fn new(sample_rate: f64) -> PyResult<Self> {
        if !sample_rate.is_finite() || sample_rate <= 0.0 {
            return Err(PyValueError::new_err("invalid sample rate"));
        }
        Ok(Self {
            sample_rate,
            delay: VecDeque::from(vec![0.0; ((0.006 * sample_rate) as usize).min(1023)]),
            detector: 0.0,
            gain: 1.0,
            reduction: 0.0,
            meter: 1.0,
            meter_rate: 1.0 - (-1.0 / (0.325 * sample_rate)).exp(),
            max_attack: -1.0,
            frame: 0,
            parameters: None,
            scaled_gain: 0.0,
            envelope_rate: 0.0,
            threshold: 0.0,
            slope: 0.0,
            knee_db: 0.0,
            knee_threshold: 0.0,
            knee_y_db: 0.0,
            k: 0.0,
            makeup: 0.0,
            release_polynomial: release_polynomial(),
        })
    }
    fn process(&mut self, value: f64, parameters: Params) -> f64 {
        self.step(value, parameters)
    }
    /// One parameter tuple for each 32-frame boundary within this block.
    fn process_block(
        &mut self,
        py: Python<'_>,
        values: Vec<f64>,
        parameters: Vec<Params>,
    ) -> PyResult<Vec<f64>> {
        let needed = (0..values.len())
            .filter(|i| (self.frame + i) % 32 == 0)
            .count();
        if parameters.len() != needed {
            return Err(PyValueError::new_err(
                "incorrect number of parameter divisions",
            ));
        }
        let count = values.len();
        let render = || {
            let mut divisions = parameters.into_iter();
            let mut current = self.parameters.unwrap_or((0.0, 0.0, 0.0, 0.0, 0.0));
            let mut output = Vec::with_capacity(values.len());
            for value in values {
                if self.frame % 32 == 0 {
                    current = divisions.next().unwrap();
                }
                output.push(self.step(value, current));
            }
            output
        };
        Ok(if count >= 4096 {
            py.detach(render)
        } else {
            render()
        })
    }
}
