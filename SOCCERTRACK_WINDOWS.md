# Testing football-dedup on SoccerTrack (Windows)

Start-to-finish steps to benchmark fixed-size vs content-defined chunking on real soccer footage from the SoccerTrack dataset. Run everything in **PowerShell**. Each part ends with a ✅ check; don't move on until it passes.

---

## Part 1: One-time setup (~10 min)

Install the tools (skip any you already have):

```powershell
winget install Git.Git
winget install Python.Python.3.12
winget install Gyan.FFmpeg
```

**Close PowerShell and open a new window** so it picks up the new programs. Then:

```powershell
cd $HOME\Documents
git clone https://github.com/Suharssh/football-dedup.git
cd football-dedup
py -m pip install -r requirements.txt
ffmpeg -version
py -m unittest -v
```

If you already cloned the repo earlier, run `git pull` inside `football-dedup` instead of cloning.

✅ `ffmpeg -version` prints a version number, and the tests end with `Ran 11 tests ... OK`.

---

## Part 2: Download SoccerTrack

1. Create a free account at https://www.kaggle.com and sign in.
2. Open https://www.kaggle.com/datasets/atomscott/soccertrack
3. **Check two things on the page first:**
   - **Total size.** 8K video can be many GB, and the benchmark needs roughly 2–3× the footage size in extra free space.
   - **License.** Confirm it allows you to describe your results publicly.
4. Click **Download** (top right). You'll get a zip file, probably `archive.zip`, in your Downloads folder.
5. Extract it into the project folder (use the real zip name if it differs):

```powershell
Expand-Archive $HOME\Downloads\archive.zip -DestinationPath soccertrack_full
```

✅ A `soccertrack_full` folder exists inside `football-dedup`.

---

## Part 3: Pick the videos to use

List every video in the dataset with its size:

```powershell
Get-ChildItem soccertrack_full -Recurse -Filter *.mp4 | Select-Object FullName, @{n="MB";e={[int]($_.Length/1MB)}}
```

You should see a **wide-view** `videos` folder (8K fish-eye camera) and possibly a **top-view** one (4K drone). **Use one folder only**: videos from different cameras can't be joined.

Copy the first 10 videos from the folder you picked into a working folder. Replace the path below with the actual folder path from the listing:

```powershell
mkdir soccertrack
Get-ChildItem soccertrack_full\wide_view\videos\*.mp4 | Select-Object -First 10 | Copy-Item -Destination soccertrack
```

Short on disk space? Use the top-view folder, or `-First 6`.

✅ `Get-ChildItem soccertrack` shows 6–10 `.mp4` files.

---

## Part 4: Run the test

```powershell
py make_clips.py soccertrack/*.mp4 --join matches/soccertrack_session.mp4
py make_clips.py matches/soccertrack_session.mp4 --out clips --per-match 8 --duration 10 --control
py dedup.py bench matches/soccertrack_session.mp4 soccertrack/*.mp4 clips/* --sweep 4096,16384,32768 --csv results.csv
```

What each command does:

1. **Join:** stitches the 30-second videos into one ~5-minute "session" file without re-encoding (ffmpeg stream copy).
2. **Clips:** cuts 8 random 10-second highlight clips from the session by stream copy, plus one **re-encoded control clip** that should share no bytes with anything.
3. **Bench:** stores all files with fixed-size 8 KB chunks, then with CDC at 8 KB (plus 4, 16 and 32 KB averages), restores and verifies every file, and prints a comparison. With 8K footage this can take several minutes.

✅ The bench finishes and prints a "Per file" table and a totals table.

---

## Part 5: Read the results

**"Per file" table** — share of each file that was already in the vault:

| File | `fixed` | `cdc` | Meaning |
|---|---|---|---|
| `soccertrack_session.mp4` | 0% | 0% | First file in; nothing to match yet |
| Original 30 s videos | ~0% | ~99% | CDC finds them inside the joined session |
| `..._clip01` … `_clip08` | ~0% | ~99% | CDC finds the highlights inside the session |
| `..._CONTROL_reencoded` | 0% | 0% | Proves the tool isn't faking matches |

**Totals table** — note these numbers:

- **Space saved** for `fixed:8192` vs `cdc:8192`
- **Restore verified** — must equal the number of files (e.g. 19/19)
- **Index size** across `cdc:4096` → `cdc:32768` — bigger chunks mean a smaller index; check how much dedup you lose for it
- **Ingest speed** — CDC is slower than fixed; that's the cost of the rolling hash

`results.csv` has the per-file numbers for every chunker, ready for a chart.

These are the real numbers for the Cohesity email. Save the full terminal output.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `py`, `git` or `ffmpeg` "not recognized" | Close and reopen PowerShell after installing |
| `--join` fails | The files came from different cameras or encodings; copy files from only one `videos` folder |
| `no files match 'soccertrack/*.mp4'` | Make sure you're in the `football-dedup` folder (`pwd`) and Part 3's copy worked |
| Disk full during bench | Use fewer files (`-First 6`) or the top-view folder; delete `clips` and rerun |
| Clips show low `cdc` numbers | Save the output — that's a real finding worth investigating, not necessarily a bug |
| Anything else | Copy the full error output and ask for help |

---

## Cleanup

When you're done:

```powershell
Remove-Item -Recurse soccertrack_full, clips
```

Video folders, clips and results are in `.gitignore`, so they're never pushed to GitHub.
