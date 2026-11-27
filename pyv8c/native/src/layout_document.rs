mod css;
mod text;

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use taffy::*;

use self::css::{get, layout_style, number, pixels};
use self::text::{FontSet, Metrics, TextCache};

type Key = u64;
// Parent zero marks the root. Slots are ordered within their composed parent.
type Row = (
    Key,
    Key,
    usize,
    String,
    Option<String>,
    css::Properties,
    Option<(f32, f32)>,
    FontSet,
);
const ANONYMOUS: Key = 1 << 63;
const VIEWPORT_CONTAINER: Key = Key::MAX;
const MAX_LAYOUT_DEPTH: usize = 1024;

struct Node {
    row: Row,
    style: Style,
    calculations: Vec<Box<css::Calculation>>,
    children: Vec<NodeId>,
    cache: Cache,
    cache_context: Option<(Size<Option<f32>>, SizingMode)>,
    layout: Layout,
}

// Taffy stores plain lengths as tagged pointers and calc handles as actual pointers.
// Every calc handle points to an immutable Box owned by this node. Moving the node
// across threads moves ownership of those allocations without changing their address.
unsafe impl Send for Node {}
// Shared access only reads the immutable calculations, and mutations require &mut Node.
unsafe impl Sync for Node {}

impl Node {
    fn new(row: Row, root_font: f32) -> Self {
        let ratio = row
            .6
            .filter(|&(w, h)| row.3 == "img" && w > 0.0 && h > 0.0)
            .map(|(w, h)| w / h);
        let mut calculations = Vec::new();
        let style = layout_style(&row.5, root_font, ratio, &mut calculations);
        Self {
            style,
            calculations,
            row,
            children: Vec::new(),
            cache: Cache::new(),
            cache_context: None,
            layout: Layout::new(),
        }
    }

    fn absolute(&self) -> bool {
        self.style.position == Position::Absolute
    }

    fn inline(&self) -> bool {
        self.row.4.is_some() || get(&self.row.5, "display").starts_with("inline")
    }

    fn lays_out_inline_children(&self) -> bool {
        matches!(get(&self.row.5, "display"), "inline" | "inline-block")
    }

    fn collapsible_whitespace(&self) -> bool {
        self.row
            .4
            .as_ref()
            .is_some_and(|text| text.chars().all(|ch| ch.is_ascii_whitespace()))
            && !matches!(
                get(&self.row.5, "white-space"),
                "pre" | "pre-wrap" | "break-spaces"
            )
    }

    fn static_axes(&self) -> Point<bool> {
        Point {
            x: self.style.inset.left.is_auto() && self.style.inset.right.is_auto(),
            y: self.style.inset.top.is_auto() && self.style.inset.bottom.is_auto(),
        }
    }
}

#[derive(Clone, Copy, Default)]
#[pyclass(frozen)]
struct Edges {
    inner: Rect<f32>,
}

#[pymethods]
impl Edges {
    #[pyo3(signature = (side, default=0.0))]
    fn get(&self, side: &str, default: f32) -> f32 {
        match side {
            "left" => self.inner.left,
            "right" => self.inner.right,
            "top" => self.inner.top,
            "bottom" => self.inner.bottom,
            _ => default,
        }
    }

    fn __getitem__(&self, side: &str) -> f32 {
        self.get(side, 0.0)
    }
}

#[derive(Clone, Copy, Default)]
#[pyclass(frozen)]
struct GeometryBox {
    #[pyo3(get)]
    left: f32,
    #[pyo3(get)]
    top: f32,
    #[pyo3(get)]
    width: f32,
    #[pyo3(get)]
    height: f32,
    #[pyo3(get)]
    overflow_right: f32,
    #[pyo3(get)]
    overflow_bottom: f32,
    #[pyo3(get)]
    padding: Edges,
    #[pyo3(get)]
    border: Edges,
    #[pyo3(get)]
    margin: Edges,
}

#[pymethods]
impl GeometryBox {
    #[getter]
    fn padding_box_left(&self) -> f32 {
        self.left + self.border.inner.left
    }
    #[getter]
    fn padding_box_top(&self) -> f32 {
        self.top + self.border.inner.top
    }
    #[getter]
    fn padding_box_width(&self) -> f32 {
        self.width - self.border.inner.left - self.border.inner.right
    }
    #[getter]
    fn padding_box_height(&self) -> f32 {
        self.height - self.border.inner.top - self.border.inner.bottom
    }
}

#[pyclass(frozen)]
struct DocumentGeometry {
    boxes: HashMap<Key, GeometryBox>,
    transforms: HashMap<Key, Transform>,
    metadata: Arc<GeometryMetadata>,
    #[pyo3(get)]
    version: u64,
}

struct GeometryMetadata {
    parents: HashMap<Key, Key>,
    offsets: HashMap<Key, Key>,
    paint_order: Vec<Key>,
    transformed: Vec<Key>,
    clips: HashMap<Key, Point<bool>>,
    clip_chains: HashMap<Key, Vec<Key>>,
}

impl DocumentGeometry {
    fn map_point(&self, key: Key, mut point: (f64, f64)) -> (f64, f64) {
        let mut current = Some(key);
        while let Some(node) = current {
            if let Some(transform) = self.transforms.get(&node) {
                point = transform.point(point);
            }
            current = self.metadata.parents.get(&node).copied();
        }
        point
    }

    fn local_point(&self, key: Key, mut point: (f64, f64)) -> Option<(f64, f64)> {
        let mut ancestors = Vec::new();
        let mut current = Some(key);
        while let Some(node) = current {
            if let Some(transform) = self.transforms.get(&node) {
                ancestors.push(transform);
            }
            current = self.metadata.parents.get(&node).copied();
        }
        for transform in ancestors.into_iter().rev() {
            point = transform.inverse_point(point)?;
        }
        Some(point)
    }
}

#[pymethods]
impl DocumentGeometry {
    fn __len__(&self) -> usize {
        self.boxes.len()
    }

    fn get(&self, key: Key) -> Option<GeometryBox> {
        self.boxes.get(&key).copied()
    }

    fn client_rect(&self, key: Key) -> Option<(f64, f64, f32, f32)> {
        let b = self.boxes.get(&key)?;
        let (left, top, width, height) = (
            f64::from(b.left),
            f64::from(b.top),
            f64::from(b.width),
            f64::from(b.height),
        );
        if self.transforms.is_empty() {
            return Some((left, top, b.width, b.height));
        }
        let mut corners = [
            (left, top),
            (left + width, top),
            (left, top + height),
            (left + width, top + height),
        ];
        let mut candidate = Some(key);
        let mut transformed = false;
        while let Some(node) = candidate {
            if let Some(transform) = self.transforms.get(&node) {
                transform.apply(&mut corners);
                transformed = true;
            }
            candidate = self.metadata.parents.get(&node).copied();
        }
        if !transformed {
            return Some((left, top, b.width, b.height));
        }
        let (l, t, r, b) = corners.iter().fold(
            (
                f64::INFINITY,
                f64::INFINITY,
                f64::NEG_INFINITY,
                f64::NEG_INFINITY,
            ),
            |(l, t, r, b), &(x, y)| (l.min(x), t.min(y), r.max(x), b.max(y)),
        );
        Some((l, t, (r - l) as f32, (b - t) as f32))
    }

    fn offset_parent(&self, key: Key) -> Option<Key> {
        self.metadata.offsets.get(&key).copied()
    }

    fn frame_point(&self, key: Key, x: f64, y: f64) -> Option<(f64, f64)> {
        let b = self.boxes.get(&key)?;
        let point = (
            x + f64::from(b.padding_box_left() + b.padding.inner.left),
            y + f64::from(b.padding_box_top() + b.padding.inner.top),
        );
        Some(if self.transforms.is_empty() {
            point
        } else {
            self.map_point(key, point)
        })
    }

    fn hit_ids(&self, x: f64, y: f64) -> Vec<Key> {
        self.metadata
            .paint_order
            .iter()
            .copied()
            .filter(|&key| {
                let local = |id| {
                    if self.transforms.is_empty() {
                        Some((x, y))
                    } else {
                        self.local_point(id, (x, y))
                    }
                };
                let Some((px, py)) = local(key) else {
                    return false;
                };
                let b = &self.boxes[&key];
                if !(f64::from(b.left) <= px
                    && px < f64::from(b.left) + f64::from(b.width)
                    && f64::from(b.top) <= py
                    && py < f64::from(b.top) + f64::from(b.height))
                {
                    return false;
                }
                self.metadata.clip_chains.get(&key).is_none_or(|chain| {
                    chain.iter().all(|id| {
                        let Some((px, py)) = local(*id) else {
                            return false;
                        };
                        let b = &self.boxes[id];
                        let axes = self.metadata.clips[id];
                        let left = f64::from(b.padding_box_left());
                        let top = f64::from(b.padding_box_top());
                        (!axes.x || (left <= px && px < left + f64::from(b.padding_box_width())))
                            && (!axes.y
                                || (top <= py && py < top + f64::from(b.padding_box_height())))
                    })
                })
            })
            .collect()
    }
}

struct Transform {
    matrix: [f64; 6],
    origin: (f64, f64),
}

impl Transform {
    fn new(style: &css::Properties, b: &GeometryBox) -> Option<Self> {
        let components = get(style, "transform")
            .strip_prefix("matrix(")?
            .strip_suffix(')')?;
        let mut parts = components.split(',');
        let mut matrix = [0.0; 6];
        for item in &mut matrix {
            *item = parts
                .next()?
                .trim()
                .parse::<f64>()
                .ok()
                .filter(|v| v.is_finite())?;
        }
        if parts.next().is_some() {
            return None;
        }
        if let Ok(angle) = get(style, "-transform-rotate").parse::<f64>() {
            let (sin, cos) = angle.to_radians().sin_cos();
            if (matrix[0] - cos).abs() < 1e-5 && (matrix[1] - sin).abs() < 1e-5 {
                matrix = [cos, sin, -sin, cos, 0.0, 0.0];
            }
        }
        let mut origin = get(style, "transform-origin").split_ascii_whitespace();
        let mut x = origin.next().unwrap_or("50%");
        let mut y = origin.next().unwrap_or("50%");
        if matches!(x, "top" | "bottom") || matches!(y, "left" | "right") {
            std::mem::swap(&mut x, &mut y);
        }
        let coerce = |value: &str, size: f32| -> f64 {
            let size = f64::from(size);
            match value {
                "top" | "left" => 0.0,
                "bottom" | "right" => size,
                "center" => size / 2.0,
                _ => {
                    if let Some(ratio) = value.strip_suffix('%') {
                        return ratio
                            .parse::<f64>()
                            .map_or(size / 2.0, |ratio| ratio * size / 100.0);
                    }
                    value
                        .strip_suffix("px")
                        .unwrap_or(value)
                        .parse()
                        .unwrap_or(size / 2.0)
                }
            }
        };
        Some(Self {
            matrix,
            origin: (
                f64::from(b.left) + coerce(x, b.width),
                f64::from(b.top) + coerce(y, b.height),
            ),
        })
    }

    fn apply(&self, corners: &mut [(f64, f64); 4]) {
        for corner in corners {
            *corner = self.point(*corner);
        }
    }

    fn point(&self, (x, y): (f64, f64)) -> (f64, f64) {
        let [a, b, c, d, e, f] = self.matrix;
        let (cx, cy) = self.origin;
        (
            cx + a * (x - cx) + c * (y - cy) + e,
            cy + b * (x - cx) + d * (y - cy) + f,
        )
    }

    fn inverse_point(&self, (x, y): (f64, f64)) -> Option<(f64, f64)> {
        let [a, b, c, d, e, f] = self.matrix;
        let det = a * d - b * c;
        if det == 0.0 {
            return None;
        }
        let (cx, cy) = self.origin;
        let (x, y) = (x - cx - e, y - cy - f);
        Some((cx + (d * x - c * y) / det, cy + (a * y - b * x) / det))
    }
}

#[derive(Default)]
#[pyclass]
pub(crate) struct LayoutDocument {
    nodes: HashMap<Key, Node>,
    // One parent in the composed input and another for anonymous formatting boxes.
    layout_parents: HashMap<Key, Key>,
    composed_children: HashMap<Key, Vec<Key>>,
    static_anchors: HashMap<Key, Key>,
    root: Key,
    depth: usize,
    root_font: f32,
    viewport: (f32, f32),
    version: u64,
    text: TextCache,
    geometry_metadata: Option<Arc<GeometryMetadata>>,
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<LayoutDocument>()?;
    module.add("EMPTY_BOX", Py::new(module.py(), GeometryBox::default())?)?;
    Ok(())
}

#[pymethods]
impl LayoutDocument {
    #[new]
    fn new() -> Self {
        Self {
            root_font: 16.0,
            ..Self::default()
        }
    }

    fn solve(
        &mut self,
        py: Python<'_>,
        base: u64,
        viewport: (f32, f32),
        rows: Vec<Row>,
        removed: Vec<Key>,
        replace: bool,
    ) -> PyResult<DocumentGeometry> {
        py.detach(|| self.apply_and_solve(base, viewport, rows, removed, replace))
            .map_err(PyValueError::new_err)
    }
}

impl LayoutDocument {
    fn validate(
        &self,
        rows: &[Row],
        removed: &HashSet<Key>,
        replace: bool,
    ) -> Result<Option<(Key, usize)>, &'static str> {
        let mut incoming = HashMap::with_capacity(rows.len());
        let mut topology = replace || !removed.is_empty();
        for row in rows {
            let (id, parent, slot, ..) = *row;
            if id == 0
                || id >= ANONYMOUS
                || parent >= ANONYMOUS
                || id == parent
                || removed.contains(&id)
                || incoming.insert(id, parent).is_some()
            {
                return Err("invalid or repeated layout node");
            }
            topology |= self
                .nodes
                .get(&id)
                .is_none_or(|node| node.row.1 != parent || node.row.2 != slot);
        }
        if !topology {
            return Ok(None);
        }

        let mut parents = incoming;
        if !replace {
            for (&id, node) in &self.nodes {
                if id < ANONYMOUS && !removed.contains(&id) {
                    parents.entry(id).or_insert(node.row.1);
                }
            }
        }
        let mut children: HashMap<Key, Vec<Key>> = HashMap::new();
        for (&id, &parent) in &parents {
            if parent != 0 && !parents.contains_key(&parent) {
                return Err("missing layout parent");
            }
            children.entry(parent).or_default().push(id);
        }
        let Some([root]) = children.get(&0).map(Vec::as_slice) else {
            return Err("layout requires one root");
        };
        let mut visited = 0;
        let mut max_depth = 0;
        let mut pending = vec![(*root, 0)];
        while let Some((id, depth)) = pending.pop() {
            if depth > MAX_LAYOUT_DEPTH {
                return Err("layout tree is too deep");
            }
            max_depth = max_depth.max(depth);
            visited += 1;
            if let Some(children) = children.get(&id) {
                pending.extend(children.iter().map(|&child| (child, depth + 1)));
            }
        }
        if visited != parents.len() {
            return Err("disconnected layout tree");
        }
        Ok(Some((*root, max_depth)))
    }

    fn apply_and_solve(
        &mut self,
        base: u64,
        viewport: (f32, f32),
        rows: Vec<Row>,
        removed: Vec<Key>,
        replace: bool,
    ) -> Result<DocumentGeometry, &'static str> {
        if base != self.version || self.version == u64::MAX || (self.root == 0 && !replace) {
            return Err("layout version mismatch");
        }
        if !viewport.0.is_finite()
            || !viewport.1.is_finite()
            || viewport.0 < 0.0
            || viewport.1 < 0.0
        {
            return Err("invalid layout viewport");
        }
        let removed: HashSet<_> = removed.into_iter().collect();
        let topology = self.validate(&rows, &removed, replace)?;
        let (_, depth) = topology.unwrap_or((self.root, self.depth));
        if depth > 128 {
            return std::thread::scope(|scope| {
                Ok(std::thread::Builder::new()
                    .name("pyv8c-layout".into())
                    .stack_size(32 * 1024 * 1024)
                    .spawn_scoped(scope, || {
                        self.apply_validated(viewport, rows, removed, replace, topology)
                    })
                    .map_err(|_| "could not allocate layout stack")?
                    .join()
                    .unwrap_or_else(|panic| std::panic::resume_unwind(panic)))
            });
        }
        Ok(self.apply_validated(viewport, rows, removed, replace, topology))
    }

    fn apply_validated(
        &mut self,
        viewport: (f32, f32),
        rows: Vec<Row>,
        removed: HashSet<Key>,
        replace: bool,
        topology: Option<(Key, usize)>,
    ) -> DocumentGeometry {
        let (root, depth) = topology.unwrap_or((self.root, self.depth));
        let mut reformat = topology.is_some();
        let mut refresh_metadata = false;
        let root_font = rows
            .iter()
            .find(|row| row.0 == root)
            .map_or(self.root_font, |row| {
                pixels(get(&row.5, "font-size"), 16.0, 16.0).unwrap_or(16.0)
            });
        if replace {
            self.nodes.clear();
        }
        for key in removed {
            self.nodes.remove(&key);
        }
        for row in rows {
            let mut node = Node::new(row, root_font);
            let id = node.row.0;
            if let Some(previous) = self.nodes.get(&id) {
                reformat |= previous.inline() != node.inline()
                    || previous.lays_out_inline_children() != node.lays_out_inline_children()
                    || previous.collapsible_whitespace() != node.collapsible_whitespace()
                    || previous.row.4.is_some() != node.row.4.is_some()
                    || previous.style.display != node.style.display
                    || previous.absolute() != node.absolute()
                    || node.absolute() && previous.static_axes() != node.static_axes()
                    || get(&previous.row.5, "position") != get(&node.row.5, "position")
                    || previous
                        .row
                        .5
                        .get("transform")
                        .is_some_and(|v| v.starts_with("matrix("))
                        != node
                            .row
                            .5
                            .get("transform")
                            .is_some_and(|v| v.starts_with("matrix("))
                    || [
                        "order",
                        "font-size",
                        "line-height",
                        "white-space",
                        "text-align",
                        "direction",
                    ]
                    .into_iter()
                    .any(|name| get(&previous.row.5, name) != get(&node.row.5, name));
                refresh_metadata |= previous.row.3 != node.row.3
                    || [
                        "-shadow-boundary",
                        "visibility",
                        "pointer-events",
                        "z-index",
                        "transform",
                        "overflow",
                        "overflow-x",
                        "overflow-y",
                    ]
                    .into_iter()
                    .any(|name| get(&previous.row.5, name) != get(&node.row.5, name));
                node.children.clone_from(&previous.children);
            }
            self.nodes.insert(id, node);
            if !reformat {
                self.mark_dirty(id);
            }
        }
        if root_font != self.root_font {
            for node in self.nodes.values_mut() {
                let mut calculations = Vec::new();
                let replacement = layout_style(
                    &node.row.5,
                    root_font,
                    node.style.aspect_ratio,
                    &mut calculations,
                );
                node.style = replacement;
                node.calculations = calculations;
                node.cache.clear();
            }
        }
        self.root_font = root_font;
        self.root = root;
        self.depth = depth;
        if reformat {
            self.rebuild_formatting_tree();
        }
        if reformat || refresh_metadata {
            self.geometry_metadata = None;
        }
        if self.viewport != viewport {
            for node in self.nodes.values_mut() {
                node.cache.clear();
            }
        }
        self.viewport = viewport;
        let available = Size {
            width: AvailableSpace::Definite(viewport.0),
            height: AvailableSpace::Definite(viewport.1),
        };
        compute_root_layout(self, root.into(), available);
        if let Some(container) = self.nodes.get_mut(&VIEWPORT_CONTAINER) {
            container.style.size = Size {
                width: Dimension::length(viewport.0),
                height: Dimension::length(viewport.1),
            };
            compute_root_layout(self, VIEWPORT_CONTAINER.into(), available);
        }
        self.version += 1;
        self.geometry()
    }

    fn mark_dirty(&mut self, mut key: Key) {
        loop {
            self.nodes.get_mut(&key).unwrap().cache.clear();
            let Some(&parent) = self.layout_parents.get(&key) else {
                break;
            };
            key = parent;
        }
    }

    fn rebuild_formatting_tree(&mut self) {
        self.nodes.retain(|&id, _| id < ANONYMOUS);
        let mut composed: HashMap<Key, Vec<(usize, Key)>> = HashMap::new();
        for (&id, node) in &mut self.nodes {
            node.children.clear();
            node.cache.clear();
            if node.row.1 != 0 {
                composed
                    .entry(node.row.1)
                    .or_default()
                    .push((node.row.2, id));
            }
        }
        self.layout_parents.clear();
        self.composed_children.clear();
        self.static_anchors.clear();
        let mut anonymous_id = ANONYMOUS;
        for (parent, mut children) in composed {
            let mode = self.nodes[&parent].style.display;
            let parent_inline = self.inline_context(parent, children.iter().map(|&(_, id)| id));
            children.sort_unstable();
            self.composed_children
                .insert(parent, children.iter().map(|&(_, id)| id).collect());
            children.sort_unstable_by_key(|&(slot, id)| {
                let order = if matches!(mode, Display::Flex | Display::Grid) {
                    number(get(&self.nodes[&id].row.5, "order")).unwrap_or(0.0) as i32
                } else {
                    0
                };
                (order, slot)
            });
            let mut grouping = None;
            for (_, child) in children {
                let node = &self.nodes[&child];
                if matches!(mode, Display::Flex | Display::Grid) && node.collapsible_whitespace() {
                    continue;
                }
                let run = !parent_inline
                    && !node.absolute()
                    && (mode == Display::Block && node.inline()
                        || matches!(mode, Display::Flex | Display::Grid) && node.row.4.is_some());
                let target = if run {
                    *grouping.get_or_insert_with(|| {
                        let id = anonymous_id;
                        anonymous_id += 1;
                        let parent_node = &self.nodes[&parent];
                        let props = [
                            "font-size",
                            "line-height",
                            "white-space",
                            "text-align",
                            "direction",
                        ]
                        .into_iter()
                        .filter_map(|name| {
                            parent_node
                                .row
                                .5
                                .get(name)
                                .map(|value| (name.to_owned(), value.clone()))
                        })
                        .collect();
                        let mut line = Node::new(
                            (id, parent, 0, String::new(), None, props, None, vec![]),
                            self.root_font,
                        );
                        line.style.display = Display::Block;
                        self.nodes.insert(id, line);
                        self.nodes
                            .get_mut(&parent)
                            .unwrap()
                            .children
                            .push(id.into());
                        self.layout_parents.insert(id, parent);
                        id
                    })
                } else {
                    grouping = None;
                    self.positioning_parent(child, parent)
                };
                let axes = self.nodes[&child].static_axes();
                if self.nodes[&child].absolute() && target != parent && (axes.x || axes.y) {
                    let anchor = anonymous_id;
                    anonymous_id += 1;
                    let mut placeholder = Node::new(
                        (
                            anchor,
                            parent,
                            0,
                            String::new(),
                            None,
                            HashMap::new(),
                            None,
                            vec![],
                        ),
                        self.root_font,
                    );
                    placeholder.style.position = Position::Absolute;
                    placeholder.style.size = Size {
                        width: Dimension::length(0.0),
                        height: Dimension::length(0.0),
                    };
                    self.nodes.insert(anchor, placeholder);
                    self.nodes
                        .get_mut(&parent)
                        .unwrap()
                        .children
                        .push(anchor.into());
                    self.layout_parents.insert(anchor, parent);
                    self.static_anchors.insert(child, anchor);
                }
                if target == VIEWPORT_CONTAINER {
                    self.nodes.entry(target).or_insert_with(|| {
                        Node::new(
                            (
                                target,
                                0,
                                0,
                                String::new(),
                                None,
                                HashMap::new(),
                                None,
                                vec![],
                            ),
                            self.root_font,
                        )
                    });
                }
                self.nodes
                    .get_mut(&target)
                    .unwrap()
                    .children
                    .push(child.into());
                self.layout_parents.insert(child, target);
            }
        }
    }

    fn inline_context(&self, key: Key, mut children: impl Iterator<Item = Key>) -> bool {
        let node = &self.nodes[&key];
        node.lays_out_inline_children()
            && children.all(|child| {
                let node = &self.nodes[&child];
                !node.absolute() && node.inline()
            })
    }

    fn positioning_parent(&self, child: Key, parent: Key) -> Key {
        let position = get(&self.nodes[&child].row.5, "position");
        match position {
            "absolute" | "fixed" => {
                let mut ancestor = parent;
                let mut containing = None;
                while ancestor != 0 {
                    let node = &self.nodes[&ancestor];
                    if node.style.display == Display::None {
                        return ancestor;
                    }
                    if get(&node.row.5, "transform").starts_with("matrix(")
                        || position == "absolute"
                            && !matches!(get(&node.row.5, "position"), "" | "static")
                    {
                        containing.get_or_insert(ancestor);
                    }
                    ancestor = node.row.1;
                }
                // The initial containing block has the viewport's dimensions,
                // independent of the root element's own box.
                containing.unwrap_or(VIEWPORT_CONTAINER)
            }
            _ => parent,
        }
    }

    fn geometry(&mut self) -> DocumentGeometry {
        let mut boxes = HashMap::with_capacity(self.nodes.len());
        let mut origins = HashMap::new();
        let mut stack = vec![(NodeId::from(self.root), Point::<f32>::ZERO)];
        if self.nodes.contains_key(&VIEWPORT_CONTAINER) {
            stack.push((VIEWPORT_CONTAINER.into(), Point::<f32>::ZERO));
        }
        while let Some((id, origin)) = stack.pop() {
            let key: Key = id.into();
            let node = &self.nodes[&key];
            if node.style.display == Display::None {
                continue;
            }
            let b = node.layout;
            let location = if self.static_anchors.is_empty() {
                origin + b.location
            } else {
                self.position_with_static_anchor(key, &mut origins)
            };
            if key < ANONYMOUS && node.row.4.is_none() {
                let geometry = GeometryBox {
                    left: location.x,
                    top: location.y,
                    width: b.size.width,
                    height: b.size.height,
                    padding: Edges { inner: b.padding },
                    border: Edges { inner: b.border },
                    margin: Edges { inner: b.margin },
                    overflow_right: b.content_size.width.max(b.size.width - b.border.right),
                    overflow_bottom: b.content_size.height.max(b.size.height - b.border.bottom),
                };
                boxes.insert(key, geometry);
            }
            stack.extend(node.children.iter().map(|&child| (child, location)));
        }
        // Size changes leave DOM ancestry and stacking intact. Older snapshots
        // keep their metadata when later changes replace it.
        if self.geometry_metadata.is_none() {
            self.geometry_metadata = Some(Arc::new(self.build_geometry_metadata(&boxes)));
        }
        let metadata = Arc::clone(self.geometry_metadata.as_ref().unwrap());
        // The transform origin still depends on each snapshot's box.
        let transforms = metadata
            .transformed
            .iter()
            .filter_map(|&key| {
                Transform::new(&self.nodes[&key].row.5, &boxes[&key])
                    .map(|transform| (key, transform))
            })
            .collect();
        DocumentGeometry {
            boxes,
            transforms,
            metadata,
            version: self.version,
        }
    }

    fn position_with_static_anchor(
        &self,
        key: Key,
        origins: &mut HashMap<Key, Point<f32>>,
    ) -> Point<f32> {
        if let Some(&point) = origins.get(&key) {
            return point;
        }
        let node = &self.nodes[&key];
        let parent = self.layout_parents.get(&key).copied();
        let mut point = parent.map_or(Point::ZERO, |id| {
            self.position_with_static_anchor(id, origins)
        }) + node.layout.location;
        if let Some(&anchor) = self.static_anchors.get(&key) {
            let static_position = self.position_with_static_anchor(anchor, origins);
            let axes = node.static_axes();
            if axes.x {
                point.x = static_position.x + node.layout.margin.left;
            }
            if axes.y {
                point.y = static_position.y + node.layout.margin.top;
            }
        }
        origins.insert(key, point);
        point
    }

    fn build_geometry_metadata(&self, boxes: &HashMap<Key, GeometryBox>) -> GeometryMetadata {
        let body = self.composed_children.get(&self.root).and_then(|children| {
            children
                .iter()
                .copied()
                .find(|child| self.nodes[child].row.3 == "body")
        });
        let mut parents = HashMap::with_capacity(boxes.len());
        let mut offsets = HashMap::with_capacity(boxes.len());
        let mut transformed = Vec::new();
        for &key in boxes.keys() {
            let node = &self.nodes[&key];
            let parent = node.row.1;
            if parent != 0 {
                parents.insert(key, parent);
            }
            if let Some(offset) = self.offset_parent(node, body) {
                offsets.insert(key, offset);
            }
            if get(&node.row.5, "transform").starts_with("matrix(") {
                transformed.push(key);
            }
        }
        let mut paint_order = Vec::with_capacity(boxes.len());
        self.paint_order(self.root, boxes, &mut paint_order);
        let clips: HashMap<_, _> = boxes
            .keys()
            .filter_map(|&key| {
                let axes = self.nodes[&key]
                    .style
                    .overflow
                    .map(|v| v != Overflow::Visible);
                (axes.x || axes.y).then_some((key, axes))
            })
            .collect();
        let mut clip_chains = HashMap::new();
        if !clips.is_empty() {
            for &key in &paint_order {
                let mut ancestors = Vec::new();
                let mut cursor = key;
                while let Some(&parent) = self.layout_parents.get(&cursor) {
                    if clips.contains_key(&parent) {
                        ancestors.push(parent);
                    }
                    cursor = parent;
                }
                if !ancestors.is_empty() {
                    clip_chains.insert(key, ancestors);
                }
            }
        }
        GeometryMetadata {
            parents,
            offsets,
            paint_order,
            transformed,
            clips,
            clip_chains,
        }
    }

    fn dom_parent(&self, node: &Node) -> Key {
        if get(&node.row.5, "-shadow-boundary") == "1" {
            0
        } else {
            node.row.1
        }
    }

    fn offset_parent(&self, node: &Node, body: Option<Key>) -> Option<Key> {
        if matches!(node.row.3.as_str(), "html" | "body") || get(&node.row.5, "position") == "fixed"
        {
            return None;
        }
        let mut parent = self.dom_parent(node);
        while let Some(node) = self.nodes.get(&parent) {
            if !matches!(get(&node.row.5, "position"), "" | "static")
                || matches!(node.row.3.as_str(), "body" | "td" | "th" | "table")
            {
                return Some(parent);
            }
            parent = self.dom_parent(node);
        }
        body
    }

    fn paint_order(&self, key: Key, boxes: &HashMap<Key, GeometryBox>, output: &mut Vec<Key>) {
        if !boxes.contains_key(&key) {
            return;
        }
        let node = &self.nodes[&key];
        if let Some(children) = self.composed_children.get(&key) {
            let mut ordered = children.clone();
            let stacks = matches!(node.style.display, Display::Flex | Display::Grid);
            ordered.sort_by_key(|child| {
                let style = &self.nodes[child].row.5;
                if stacks || !matches!(get(style, "position"), "" | "static") {
                    get(style, "z-index").parse::<i64>().unwrap_or(0)
                } else {
                    0
                }
            });
            for child in ordered.into_iter().rev() {
                self.paint_order(child, boxes, output);
            }
        }
        let style = &node.row.5;
        if !matches!(get(style, "visibility"), "hidden" | "collapse")
            && get(style, "pointer-events") != "none"
        {
            output.push(key);
        }
    }

    fn leaf(&mut self, id: NodeId, input: LayoutInput) -> LayoutOutput {
        let node = &self.nodes[&id.into()];
        let mut metrics = None;
        let mut output =
            compute_leaf_layout(input, &node.style, css::resolve_calc, |known, available| {
                if let Some(value) = &node.row.4 {
                    let measured =
                        self.text
                            .measure(value, &node.row.5, &node.row.7, self.root_font);
                    let value = measured.layout(
                        known
                            .width
                            .map_or(available.width, AvailableSpace::Definite),
                    );
                    metrics = Some(value);
                    value.size
                } else if let Some((width, height)) = node.row.6 {
                    Size { width, height }
                } else if node.row.3 == "input" {
                    self.text
                        .control(&node.row.5, &node.row.7, self.root_font)
                        .unwrap_or(Size::ZERO)
                } else {
                    Size::ZERO
                }
            });
        if let Some(metrics) = metrics {
            output.first_baselines.y = Some(metrics.baseline);
        }
        output
    }

    fn inline_layout(&mut self, id: NodeId, input: LayoutInput) -> LayoutOutput {
        let node = &self.nodes[&id.into()];
        let children = node.children.clone();
        let style = node.style.clone();
        let align = get(&node.row.5, "text-align").to_owned();
        let rtl = get(&node.row.5, "direction") == "rtl";
        let wraps = !matches!(get(&node.row.5, "white-space"), "nowrap" | "pre");
        let strut = self
            .text
            .measure(" ", &node.row.5, &node.row.7, self.root_font)
            .strut();
        let parent_width = input.parent_size.width.unwrap_or(0.0);
        let use_parent_height =
            Key::from(id) >= ANONYMOUS || get(&node.row.5, "display") == "inline";
        let has_definite_height = input.known_dimensions.height.is_some()
            || style
                .size
                .height
                .maybe_resolve(input.parent_size.height, css::resolve_calc)
                .is_some();
        let padding = resolved_edges(style.padding, parent_width);
        let border = resolved_edges(style.border, parent_width);
        let origin = Point {
            x: padding.left + border.left,
            y: padding.top + border.top,
        };
        let mut metrics = Metrics::default();
        let mut output =
            compute_leaf_layout(input, &style, css::resolve_calc, |known, available| {
                let space = known
                    .width
                    .map_or(available.width, AvailableSpace::Definite);
                let parent_height = if use_parent_height {
                    input.parent_size.height
                } else if has_definite_height {
                    available.height.into_option()
                } else {
                    None
                };
                metrics = self.compose_lines(
                    &children,
                    input.run_mode,
                    LineSpace {
                        space,
                        parent_height,
                        origin,
                        strut,
                        wraps,
                        align: &align,
                        rtl,
                    },
                );
                metrics.size
            });
        if metrics.size.height > 0.0 {
            output.first_baselines.y = Some(metrics.baseline + origin.y);
        }
        output
    }

    fn compose_lines(
        &mut self,
        children: &[NodeId],
        mode: RunMode,
        line: LineSpace<'_>,
    ) -> Metrics {
        let limit = match line.space {
            AvailableSpace::Definite(width) => width,
            AvailableSpace::MinContent if line.wraps => 0.0,
            _ => f32::INFINITY,
        };
        let basis = if limit.is_finite() { limit } else { 0.0 };
        let place =
            |tree: &mut Self, items: &mut Vec<InlineItem>, width: f32, ascent: f32, top: f32| {
                let unused = if limit.is_finite() {
                    (limit - width).max(0.0)
                } else {
                    0.0
                };
                let shift = match line.align {
                    "center" => unused / 2.0,
                    "right" => unused,
                    "end" if !line.rtl => unused,
                    "start" | "" if line.rtl => unused,
                    _ => 0.0,
                };
                for item in items.drain(..) {
                    let node = tree.nodes.get_mut(&item.id.into()).unwrap();
                    let style = &node.style;
                    let baseline = item
                        .geometry
                        .first_baselines
                        .y
                        .unwrap_or(item.geometry.size.height);
                    let x = if line.rtl {
                        width - item.left - item.geometry.size.width
                    } else {
                        item.left
                    };
                    let inset = |near, far, basis| {
                        resolve_auto(near, basis)
                            .or_else(|| resolve_auto(far, basis).map(|v| -v))
                            .unwrap_or(0.0)
                    };
                    node.layout = Layout {
                        location: Point {
                            x: line.origin.x
                                + x
                                + shift
                                + inset(style.inset.left, style.inset.right, basis),
                            y: line.origin.y + top + ascent - baseline
                                + inset(
                                    style.inset.top,
                                    style.inset.bottom,
                                    line.parent_height.unwrap_or(0.0),
                                ),
                        },
                        size: item.geometry.size,
                        content_size: item.geometry.content_size,
                        padding: resolved_edges(style.padding, basis),
                        border: resolved_edges(style.border, basis),
                        margin: style
                            .margin
                            .map(|value| resolve_auto(value, basis).unwrap_or(0.0)),
                        ..Layout::new()
                    };
                }
            };
        let mut items = Vec::new();
        let (mut width, mut ascent, mut descent, mut top, mut widest) =
            (0.0f32, 0.0f32, 0.0f32, 0.0f32, 0.0f32);
        let mut first = None;
        let joined = self.joined_inline_widths(children);
        for (index, &id) in children.iter().enumerate() {
            let node = &self.nodes[&id.into()];
            if node.style.display == Display::None {
                continue;
            }
            let forced_break = node.row.3 == "br";
            let text = node.row.4.is_some();
            if let Some(value) = node
                .row
                .4
                .as_ref()
                .filter(|value| value.chars().all(|ch| ch.is_ascii_whitespace()))
            {
                if width > 0.0
                    && !matches!(
                        get(&node.row.5, "white-space"),
                        "pre" | "pre-wrap" | "pre-line" | "break-spaces"
                    )
                {
                    width += self
                        .text
                        .measure(value, &node.row.5, &node.row.7, self.root_font)
                        .untrimmed_width();
                    continue;
                }
            }
            let space = if text && line.wraps && width == 0.0 {
                line.space
            } else {
                AvailableSpace::MaxContent
            };
            let margin = node
                .style
                .margin
                .map(|value| resolve_auto(value, basis).unwrap_or(0.0));
            let atomic = get(&node.row.5, "display") != "inline"
                || node.row.6.is_some()
                || node.row.3 == "input";
            let output = self.compute_child_layout(
                id,
                LayoutInput {
                    run_mode: mode,
                    available_space: Size {
                        width: space,
                        height: AvailableSpace::MaxContent,
                    },
                    known_dimensions: Size {
                        width: joined.get(&index).copied(),
                        height: None,
                    },
                    parent_size: Size {
                        width: limit.is_finite().then_some(limit),
                        height: line.parent_height,
                    },
                    ..LayoutInput::HIDDEN
                },
            );
            let outer_width = output.size.width + margin.left + margin.right;
            if width > 0.0 && line.wraps && width + outer_width > limit || forced_break {
                if forced_break && ascent + descent == 0.0 {
                    let node = &self.nodes[&id.into()];
                    let strut = self
                        .text
                        .measure(" ", &node.row.5, &node.row.7, self.root_font)
                        .strut();
                    ascent = strut.baseline;
                    descent = strut.size.height - ascent;
                }
                first.get_or_insert(ascent);
                if mode == RunMode::PerformLayout {
                    place(self, &mut items, width, ascent, top);
                }
                top += ascent + descent;
                widest = widest.max(width);
                (width, ascent, descent) = (0.0, 0.0, 0.0);
            }
            if !forced_break {
                let baseline = output.first_baselines.y.unwrap_or(output.size.height);
                if !text && atomic && output.size.height == 0.0 {
                    ascent = ascent.max(line.strut.baseline);
                    descent = descent.max(line.strut.size.height - line.strut.baseline);
                }
                ascent = ascent.max(baseline + margin.top);
                descent = descent.max(output.size.height - baseline + margin.bottom);
                if mode == RunMode::PerformLayout && !text {
                    items.push(InlineItem {
                        id,
                        geometry: output,
                        left: width + margin.left,
                    });
                }
                width += outer_width;
            }
        }
        if mode == RunMode::PerformLayout {
            place(self, &mut items, width, ascent, top);
        }
        Metrics {
            size: Size {
                width: widest.max(width),
                height: top + ascent + descent,
            },
            baseline: first.unwrap_or(ascent),
        }
    }

    fn joined_inline_widths(&mut self, children: &[NodeId]) -> HashMap<usize, f32> {
        let text_node = |nodes: &HashMap<Key, Node>, id: NodeId| -> Option<NodeId> {
            let node = &nodes[&id.into()];
            if get(&node.row.5, "display") != "inline"
                || !node.style.size.width.is_auto()
                || node.style.border != Rect::zero()
                || node.style.padding != Rect::zero()
                || node.style.margin != Rect::zero()
            {
                return None;
            }
            let [child] = node.children.as_slice() else {
                return None;
            };
            nodes[&Key::from(*child)]
                .row
                .4
                .as_ref()
                .filter(|value| !value.is_empty())
                .map(|_| *child)
        };
        let mut widths = HashMap::new();
        let mut start = 0;
        while start + 1 < children.len() {
            let Some(first) = text_node(&self.nodes, children[start]) else {
                start += 1;
                continue;
            };
            let base = &self.nodes[&first.into()].row;
            let mut parts = vec![base.4.as_deref().unwrap()];
            for &item in &children[start + 1..] {
                let Some(text) = text_node(&self.nodes, item) else {
                    break;
                };
                let row = &self.nodes[&text.into()].row;
                if row.5 != base.5 || row.7 != base.7 {
                    break;
                }
                parts.push(row.4.as_deref().unwrap());
            }
            if parts.len() > 1 {
                let measured = self
                    .text
                    .adjacent_widths(&parts, &base.5, &base.7, self.root_font);
                widths.extend((start..).zip(measured));
            }
            start += parts.len();
        }
        widths
    }
}

struct LineSpace<'a> {
    space: AvailableSpace,
    parent_height: Option<f32>,
    origin: Point<f32>,
    strut: Metrics,
    wraps: bool,
    align: &'a str,
    rtl: bool,
}

struct InlineItem {
    id: NodeId,
    geometry: LayoutOutput,
    left: f32,
}

fn resolve_auto(value: LengthPercentageAuto, basis: f32) -> Option<f32> {
    value.resolve_to_option(basis, css::resolve_calc)
}

fn resolved_edges(edges: Rect<LengthPercentage>, basis: f32) -> Rect<f32> {
    edges.resolve_or_zero(Some(basis), css::resolve_calc)
}

impl TraversePartialTree for LayoutDocument {
    type ChildIter<'a> = std::iter::Copied<std::slice::Iter<'a, NodeId>>;
    fn child_ids(&self, id: NodeId) -> Self::ChildIter<'_> {
        self.nodes[&id.into()].children.iter().copied()
    }
    fn child_count(&self, id: NodeId) -> usize {
        self.nodes[&id.into()].children.len()
    }
    fn get_child_id(&self, id: NodeId, index: usize) -> NodeId {
        self.nodes[&id.into()].children[index]
    }
}

impl LayoutPartialTree for LayoutDocument {
    type CustomIdent = String;
    type CoreContainerStyle<'a> = &'a Style;
    fn resolve_calc_value(&self, handle: *const (), basis: f32) -> f32 {
        css::resolve_calc(handle, basis)
    }
    fn get_core_container_style(&self, id: NodeId) -> &Style {
        &self.nodes[&id.into()].style
    }
    fn set_unrounded_layout(&mut self, id: NodeId, layout: &Layout) {
        self.nodes.get_mut(&id.into()).unwrap().layout = *layout;
    }
    fn compute_child_layout(&mut self, id: NodeId, input: LayoutInput) -> LayoutOutput {
        if input.run_mode == RunMode::PerformHiddenLayout {
            return compute_hidden_layout(self, id);
        }
        compute_cached_layout(self, id, input, |tree, id, input| {
            let node = &tree.nodes[&id.into()];
            match node.style.display {
                Display::None => compute_hidden_layout(tree, id),
                _ if node.children.is_empty() => tree.leaf(id, input),
                Display::Flex => compute_flexbox_layout(tree, id, input),
                Display::Grid => compute_grid_layout(tree, id, input),
                _ if (ANONYMOUS..VIEWPORT_CONTAINER).contains(&Key::from(id))
                    || tree.inline_context(
                        id.into(),
                        tree.composed_children
                            .get(&id.into())
                            .into_iter()
                            .flatten()
                            .copied(),
                    ) =>
                {
                    tree.inline_layout(id, input)
                }
                _ => compute_block_layout(tree, id, input, None),
            }
        })
    }
}

impl CacheTree for LayoutDocument {
    fn cache_get(&self, id: NodeId, input: &LayoutInput) -> Option<LayoutOutput> {
        let node = &self.nodes[&id.into()];
        // Taffy's cache keys include available space, which can stay unchanged
        // while a definite percentage basis changes.
        if node.cache_context == Some((input.parent_size, input.sizing_mode)) {
            node.cache.get(input)
        } else {
            None
        }
    }
    fn cache_store(&mut self, id: NodeId, input: &LayoutInput, output: LayoutOutput) {
        let node = self.nodes.get_mut(&id.into()).unwrap();
        let context = (input.parent_size, input.sizing_mode);
        if node.cache_context != Some(context) {
            node.cache.clear();
            node.cache_context = Some(context);
        }
        node.cache.store(input, output);
    }
    fn cache_clear(&mut self, id: NodeId) {
        self.nodes.get_mut(&id.into()).unwrap().cache.clear();
    }
}

impl LayoutBlockContainer for LayoutDocument {
    type BlockContainerStyle<'a> = &'a Style;
    type BlockItemStyle<'a> = &'a Style;
    fn get_block_container_style(&self, id: NodeId) -> &Style {
        &self.nodes[&id.into()].style
    }
    fn get_block_child_style(&self, id: NodeId) -> &Style {
        &self.nodes[&id.into()].style
    }
}

impl LayoutFlexboxContainer for LayoutDocument {
    type FlexboxContainerStyle<'a> = &'a Style;
    type FlexboxItemStyle<'a> = &'a Style;
    fn get_flexbox_container_style(&self, id: NodeId) -> &Style {
        &self.nodes[&id.into()].style
    }
    fn get_flexbox_child_style(&self, id: NodeId) -> &Style {
        &self.nodes[&id.into()].style
    }
}

impl LayoutGridContainer for LayoutDocument {
    type GridContainerStyle<'a> = &'a Style;
    type GridItemStyle<'a> = &'a Style;
    fn get_grid_container_style(&self, id: NodeId) -> &Style {
        &self.nodes[&id.into()].style
    }
    fn get_grid_child_style(&self, id: NodeId) -> &Style {
        &self.nodes[&id.into()].style
    }
}
