# Putting the converter online (non-technical guide)

This gets you a normal web link you open in any browser, on a **free** host
(Streamlit Community Cloud). Budget about 15 minutes. You don't need to write
any code — just clicking and pasting.

There are two parts:

- **Part 1 — Go live.** Gets the app running with a link. ~5 min.
- **Part 2 — Make your lists stick (recommended).** Connects a free database so
  your item/customer lists survive restarts. ~10 min. Skip it just to try the
  app; do it before real daily use.

---

## Part 1 — Go live on Streamlit Community Cloud

1. Go to **https://streamlit.io/cloud** and click **Sign up** (or **Continue
   with GitHub**). Use the GitHub account that owns this project (`Rabbi`).
2. Click **Create app** → **Deploy a public app from GitHub**.
3. Fill in:
   - **Repository:** `raphaelooladokun-ops/Rabbi`
   - **Branch:** `claude/einvoicing-converter-brief-dve1x1`
   - **Main file path:** `app.py`
4. Before clicking Deploy, open **Advanced settings → Secrets** and paste your
   login line (see **Setting your login** just below). Then click **Deploy**.
5. Wait ~2 minutes while it installs. You'll get a link like
   `https://rabbi-xxxx.streamlit.app` — that's your app. Bookmark it.

### Setting your login

On your own computer (one time), in the project folder, run:

```
python scripts/make_login.py
```

It asks for a username and password and prints **one line** starting with
`RABBI_USERS = ...`. Paste that whole line into the **Secrets** box in step 4.

> If you skip this, the app still runs but uses the default login
> `admin` / `rabbi-change-me` and shows a warning. Don't use the default for
> real client data.

That's it — you can sign in and use it. **But** until you do Part 2, anything
you import or add (your item/customer lists) will be wiped whenever the app
restarts. For a real workflow, do Part 2.

---

## Part 2 — Make your lists persist (free database)

Streamlit's free host wipes its disk on every restart, so your masters need to
live in a small database. Neon gives one away free, no card needed.

1. Go to **https://neon.tech** → **Sign up** (GitHub login is fine).
2. Click **Create project** (any name, e.g. `rabbi`). Accept the defaults.
3. On the project dashboard, find **Connection string** and click **Copy**. It
   looks like:
   `postgresql://user:password@ep-xxxx.neon.tech/dbname?sslmode=require`
4. Back in your Streamlit app: **Manage app → Settings → Secrets**. Add a new
   line below your login line:

   ```
   DATABASE_URL = "postgresql://...paste the whole Neon string here..."
   ```
5. Save. The app restarts automatically. From now on your item/customer lists
   are stored in the database and persist forever.

After this, go to the **Master data** tab and **import your items list and
customers** once (the importer reads your existing helper sheets as-is). You're
set for daily use.

---

## Day-to-day, once it's live

1. Open your app link and sign in.
2. **Master data** tab → import/maintain each client's item and customer lists
   (only needed the first time, and whenever you add new ones).
3. **Convert** tab → pick the client → upload their raw sales file →
   review anything flagged → **Download CSV** → upload that CSV to Digitax.

## If something looks wrong

- **"No operator accounts configured" warning** → you haven't set `RABBI_USERS`
  yet (see Setting your login).
- **Lists disappear after a while** → you haven't done Part 2 (the database).
- **App is asleep** → free apps sleep after inactivity; opening the link wakes
  it in a few seconds.
- Changes you ask me to make are pushed to the same branch; in the app use
  **Manage app → Reboot** to pick them up.

## A note on privacy

Your client data (TINs, item codes) will live on Streamlit's and Neon's free
servers. That's normal for an internal tool, but if you'd prefer nothing leaves
your own computer, tell me and I'll give you the "run on my laptop" version
instead.
