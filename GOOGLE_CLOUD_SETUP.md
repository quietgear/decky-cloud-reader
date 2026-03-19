# Google Cloud Setup Guide

This guide walks you through setting up Google Cloud services for Decky Cloud Reader. No prior Google Cloud experience required.

> **Note:** Google Cloud is **optional**. The plugin works out of the box in **local mode** (offline OCR + TTS) without any cloud setup. Only follow this guide if you want to use GCP for higher accuracy OCR or more natural-sounding voices.

## What You'll Set Up

- **Cloud Vision API** — reads text from screenshots (OCR)
- **Cloud Text-to-Speech API** — converts text to spoken audio

## Cost

- **Free tier**: 1,000 OCR requests/month + 1M characters TTS/month (always free, no expiration)
- **New accounts**: $300 free credits for 90 days
- **Typical usage**: Completely free for personal use

---

## Step 1: Create a Google Cloud Account

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Sign in with your Google account (or create one)
3. Accept the terms of service
4. New users get **$300 free credits** valid for 90 days

---

## Step 2: Create a Project

1. Click the project dropdown at the top-left (next to "Google Cloud" logo)
2. Click **"New Project"**
3. Enter a name: `decky-cloud-reader` (or anything you like)
4. Click **"Create"**
5. Wait for the project to be created, then make sure it's selected in the dropdown

---

## Step 3: Enable the APIs

You need to enable two APIs for this project.

### Enable Cloud Vision API

1. Go to [Cloud Vision API](https://console.cloud.google.com/apis/library/vision.googleapis.com)
2. Make sure your project is selected at the top
3. Click **"Enable"**

### Enable Cloud Text-to-Speech API

1. Go to [Cloud Text-to-Speech API](https://console.cloud.google.com/apis/library/texttospeech.googleapis.com)
2. Make sure your project is selected at the top
3. Click **"Enable"**

---

## Step 4: Create a Service Account

A service account is like a special user account for your application.

1. Go to [Service Accounts](https://console.cloud.google.com/iam-admin/serviceaccounts)
2. Click **"+ Create Service Account"**
3. Fill in:
   - **Service account name**: `cloud-reader-service`
   - **Description**: `Service account for OCR and TTS`
4. Click **"Create and Continue"**

### Add Permissions

5. Click **"Select a role"**
6. Search for `Cloud Vision User` and select it
7. Click **"+ Add Another Role"**
8. Search for `Cloud Text-to-Speech User` and select it
9. Click **"Continue"**
10. Click **"Done"**

---

## Step 5: Create and Download the JSON Key

1. In the Service Accounts list, click on your new account (`cloud-reader-service@...`)
2. Go to the **"Keys"** tab
3. Click **"Add Key"** > **"Create new key"**
4. Select **"JSON"**
5. Click **"Create"**

A JSON file will download automatically. **Keep this file safe!** This is your only copy.

---

## Step 6: Transfer the JSON Key to Your Steam Deck

You need to get the downloaded JSON file onto your Steam Deck. Choose any method:

| Method | How |
|--------|-----|
| **USB Drive** | Copy the JSON file to a USB stick, plug into the Deck — file will be at `/run/media/deck/<USB_NAME>/` |
| **KDE Connect** | Pair your phone or PC wirelessly (preinstalled on SteamOS), send the file — it lands in `~/Downloads/` |
| **SCP** | From another computer: `scp your-key-file.json deck@<deck-ip>:~/Downloads/` |
| **Browser** | Email the file to yourself, open Firefox in Desktop Mode, download it |

> **Tip:** The `~/Downloads/` folder is the easiest target.

---

## Step 7: Load Credentials in the Plugin

1. Switch to **Gaming Mode**
2. Press the **...** (Quick Access) button > **Decky** tab (plug icon)
3. Open **Decky Cloud Reader**
4. Set **OCR Provider** and/or **TTS Provider** to **Google Cloud**
5. Scroll down to the **GCP Credentials** section
6. Click **"Load Credentials"**
7. The built-in file browser will open — navigate to where you saved the JSON file (e.g., `Downloads/`)
8. Select the JSON file

The plugin validates the file automatically. You should see **Status: Configured** with your GCP project ID displayed.

---

## Step 8: Set Up Budget Alerts (Recommended)

Protect yourself from unexpected charges:

1. Go to [Budgets & Alerts](https://console.cloud.google.com/billing/budgets)
2. Click **"Create Budget"**
3. Configure:
   - **Name**: `Cloud Reader Monthly Limit`
   - **Projects**: Select your project
   - **Budget amount**: `$1` (or whatever you're comfortable with)
4. Set alert thresholds at **50%**, **90%**, and **100%**
5. Make sure email notifications are enabled
6. Click **"Finish"**

---

## Free Tier Limits

These limits reset monthly and **never expire**:

| Service | Free Tier |
|---------|-----------|
| Cloud Vision (OCR) | 1,000 units/month |
| Text-to-Speech (Standard voices) | 4 million characters/month |
| Text-to-Speech (WaveNet/Neural voices) | 1 million characters/month |

For typical screen reader usage, you'll stay well within free limits.

---

## Troubleshooting

### "Permission denied" or "API not enabled"

- Make sure all required APIs are enabled (Step 3)
- Make sure your service account has all required roles (Step 4)

### "Invalid credentials"

- Re-download the JSON key file (Step 5)
- Make sure you selected the correct file in the file browser

### "Quota exceeded"

- You've hit the free tier limit for the month
- Wait until the next month, or enable billing for additional usage

### How to check your usage

1. Go to [APIs & Services Dashboard](https://console.cloud.google.com/apis/dashboard)
2. Click on "Cloud Vision API" or "Cloud Text-to-Speech API"
3. View the "Metrics" tab to see your usage

---

## Security Best Practices

1. **Never share your JSON key file** or credentials publicly
2. **Don't commit credentials to git** — the `.gitignore` already excludes JSON files
3. **Use minimal permissions** — the service account only has Vision and TTS access
4. **Set up budget alerts** to catch unexpected usage
5. **Rotate keys periodically** — delete old keys and create new ones in the Cloud Console

---

## Removing GCP Access

### Clear credentials from the plugin

1. Open the plugin in Decky
2. In the **GCP Credentials** section, click **"Clear Credentials"**
3. Switch providers back to **Local**

### Delete the Google Cloud project entirely

1. Go to [Settings](https://console.cloud.google.com/iam-admin/settings)
2. Click **"Shut down"**
3. Enter your project ID to confirm
4. Click **"Shut down"**

This permanently deletes the project and all associated resources.
