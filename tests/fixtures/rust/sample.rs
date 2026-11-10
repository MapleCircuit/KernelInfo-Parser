// SPDX-License-Identifier: GPL-2.0

//! Crate documentation,
//! two lines.
#![allow(dead_code)]

/* Block comment /* nested */ still the comment */

/// Struct doc
/// second line
#[derive(Debug)]
pub(crate) struct Foo<'a, T: Clone> where T: Copy { pub a: &'a mut T, b: [u8; 4], c: Vec<Option<u32>> }

pub struct Tup(pub u32, *const u8);
pub struct Unit;

pub enum E { A, B(u64), C { x: i32 } = 5 }

/* café */ pub fn after_utf8() {}

#[no_mangle]
pub unsafe extern "C" fn ffi(x: i32, ...) -> i32 { 0 }

pub(super) const fn cf<T>(&self, mut y: impl Fn(u8) -> u8, (a, b): (u8, u8), r#type: u8) -> Result<(), Error> where T: Send { }

pub async fn later() {}

impl<T> Drop for Foo<'_, T> { fn drop(&mut self) {} }

unsafe impl Send for Unit {}
impl !Sync for Tup {}

impl Foo<'static, u8> {
    /// Method doc
    #[cfg(CONFIG_X86)]
    pub fn new() -> Self { todo!() }
    fn private(&self) {}
}

pub trait Tr: Send + Sync { type A; const N: usize = 3; fn m(&self); }

pub mod inner {
    pub fn f() {}
    mod deep;
}
mod sub;
#[path = "sample/sub/other.rs"]
mod renamed;
mod missing;

use kernel::{prelude::*, sync::Arc as A};
pub use core::mem;
extern crate alloc;

macro_rules! mm { () => {} }
module! {
    type: X,
    name: "x",
    authors: ["A <a@b.c>", "B"], // two authors
    license: "GPL",
    params: {
        p: i64 { default: 1, description: "a, b" },
    },
}

extern "C" { fn ext(x: i32) -> i32; static EXTV: u32; }

static mut S: u32 = 0; // trailing comment
type R<T> = core::result::Result<T, Error>;
union U { a: u32, b: f32 }
const _: () = ();

#[export_name = "c_name"]
fn exported(p: *mut bindings::page) -> bindings::gfp_t {
    // bindings::not_a_use()
    let s = "bindings::nor_this";
    unsafe { bindings::kfree(p.cast()) };
    let _ = kernel::bindings::GFP_KERNEL | uapi::BMCR_FULLDPLX as u32;
    let _ = bindings::phy_state_PHY_RUNNING;
    bindings::pci_device_id { vendor: 0 }
}
#[unsafe(no_mangle)]
static mut CVAR: u32 = 0;
