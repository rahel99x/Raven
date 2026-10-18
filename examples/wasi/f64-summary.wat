;; Raven f64-summary/wasi-preview1-v1: sequential IEEE754 sum, min and max.
;; Input: little-endian u32 count followed by count f64 values.
;; Output: little-endian u64 count, f64 sum, f64 minimum, f64 maximum.
(module
  (import "wasi_snapshot_preview1" "fd_read" (func $read (param i32 i32 i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "fd_write" (func $write (param i32 i32 i32 i32) (result i32)))
  (memory (export "memory") 9 9)
  (func (export "_start")
    (local $n i32) (local $i i32) (local $v f64)
    (local $sum f64) (local $min f64) (local $max f64)
    (i32.store (i32.const 0) (i32.const 64))
    (i32.store (i32.const 4) (i32.const 524284))
    (drop (call $read (i32.const 0) (i32.const 0) (i32.const 1) (i32.const 8)))
    (local.set $n (i32.load (i32.const 64)))
    (local.set $min (f64.load (i32.const 68)))
    (local.set $max (local.get $min))
    (loop $values
      (local.set $v (f64.load (i32.add (i32.const 68) (i32.mul (local.get $i) (i32.const 8)))))
      (local.set $sum (f64.add (local.get $sum) (local.get $v)))
      ;; Match Python's first-value tie rule, including signed zero.
      (if (f64.lt (local.get $v) (local.get $min)) (then (local.set $min (local.get $v))))
      (if (f64.gt (local.get $v) (local.get $max)) (then (local.set $max (local.get $v))))
      (local.set $i (i32.add (local.get $i) (i32.const 1)))
      (br_if $values (i32.lt_u (local.get $i) (local.get $n))))
    (i64.store (i32.const 16) (i64.extend_i32_u (local.get $n)))
    (f64.store (i32.const 24) (local.get $sum))
    (f64.store (i32.const 32) (local.get $min))
    (f64.store (i32.const 40) (local.get $max))
    (i32.store (i32.const 0) (i32.const 16))
    (i32.store (i32.const 4) (i32.const 32))
    (drop (call $write (i32.const 1) (i32.const 0) (i32.const 1) (i32.const 8)))))
