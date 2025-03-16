# CPU optimize tuning

## AMD Ryzen 7950X

Go to BIOS and turn on overclocking and change
`Advanced -> AMD Overclocking -> AMD Overclocking -> Precision Boost Overdrive` from `auto` to
advance. Then in `Curve Optimizer` select `all cores` and change sign from `positive` to `negative`.
Start testing with a magnitude of 30 and if it is not stable go down.

E.g. for testing CPU:

```bash
sysbench --threads="$(nproc)" cpu run --cpu-max-prime=2000000000
```

Additionally, you can change `Platform Thermal Throttle Ctrl` to manual and set power limit to 85W.
I didn't apply this.

Ref: https://www.youtube.com/watch?v=FaOYYHNGlLs
