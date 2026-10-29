# Assets the render step needs

Rendering fails with a clear message when an asset that is switched on is missing (an unbranded clip must not go out by
accident). Relative paths in the config are looked up here, or under `$ASSETS_DIR` when that is set.

| File | Used by | Notes |
| --- | --- | --- |
| `fonts/Montserrat-ExtraBold.ttf` | captions, hook, credit (`captions.font`) | Montserrat is under the SIL Open Font License, which allows commercial use. Keep its `OFL.txt` next to the file. Any other font you may use works: point `captions.font` at it. |
| `logo.png` | the logo overlay (`branding.logo`) | A PNG, ideally with a transparent background and at least 560 px wide (it is drawn 140 px wide). |

Until you have them, switch the overlay off in the profile instead of leaving it half-configured:

```yaml
captions: { enabled: false }          # or set captions.font to a font file you have
branding:
  logo: { enabled: false }            # the MLBB profile ships with the logo off for this reason
```

Captions, hook and credit are drawn inside the profile's `safe_zone`, and the logo sits in a corner of it.
