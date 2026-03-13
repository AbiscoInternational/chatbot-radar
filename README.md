# ChatbotRadar - Shopify Chatbot Detector

A production-ready web application to detect Shopify Inbox and other chatbot platforms on any Shopify store. Built with Flask and optimized for high-volume batch processing.

## Features

- **Single URL Analysis**: Quickly scan individual stores
- **Bulk Analysis**: Upload CSV/TXT files with thousands of URLs
- **Pause/Resume/Stop**: Full control over batch processing
- **Filtered Exports**: Download results by Shopify Inbox presence
- **TXT Export**: Get clean URL lists for easy reprocessing
- **Re-analyze**: Automatically retry stores without Shopify Inbox
- **Real-time Progress**: Live updates during bulk scans
- **Auto-cleanup**: Results expire after 24 hours to save memory

## Supported Chatbots

- Shopify Inbox
- Tidio
- Zendesk
- Intercom
- Drift
- Gorgias
- Crisp
- LiveChat
- Freshchat
- Chatty
- Willdesk
- Smartbot

## Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally
python app.py
```

Visit `http://localhost:5001`

## Deployment to Render

### Step 1: Prepare Your Repository

1. Create a new GitHub repository
2. Upload all files from this project:
   - `app.py`
   - `requirements.txt`
   - `runtime.txt`
   - `Procfile`
   - `templates/` folder (base.html, index.html, single_result.html, bulk_progress.html, bulk_results.html)
   - `static/` folder (if you have custom CSS/JS)
   - `.gitignore`

3. Push to GitHub:
```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/yourusername/chatbot-radar.git
git push -u origin main
```

### Step 2: Deploy on Render

1. Go to [render.com](https://render.com) and sign up/login
2. Click **"New +"** → **"Web Service"**
3. Connect your GitHub account and select your repository
4. Configure the service:

   **Basic Settings:**
   - Name: `chatbot-radar` (or your preferred name)
   - Region: Choose closest to your target audience
   - Branch: `main`
   - Root Directory: (leave empty)
   - Runtime: `Python 3`
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app --workers 4 --threads 2 --worker-class gthread --timeout 120 --keep-alive 5 --log-level info`

   **Instance Type:**
   - For testing: `Free` (512MB RAM, spins down after inactivity)
   - For production: `Starter` or higher ($7/month, 512MB RAM, always on)
   - For high volume: `Standard` ($25/month, 2GB RAM)

   **Environment Variables:**
   - Click **"Advanced"** → **"Add Environment Variable"**
   - Add: `SECRET_KEY` = (generate with: `python -c "import secrets; print(secrets.token_hex(32))"`)
   - Add: `PYTHON_VERSION` = `3.11.7`

5. Click **"Create Web Service"**

6. Wait for deployment (3-5 minutes)

7. Your app will be live at: `https://chatbot-radar.onrender.com` (or your custom name)

### Step 3: Post-Deployment

1. **Test the app**: Visit your Render URL and run a test scan
2. **Monitor logs**: Click on "Logs" in Render dashboard to see real-time activity
3. **Set up custom domain** (optional):
   - Go to Settings → Custom Domain
   - Add your domain and configure DNS

### Step 4: Upgrade for Production (Recommended)

For heavy usage, upgrade your instance:

1. Go to your service in Render dashboard
2. Click **"Settings"** → **"Instance Type"**
3. Choose **"Standard"** or **"Pro"** for:
   - More memory (2GB - 16GB)
   - Better CPU
   - No cold starts
   - Higher request limits

## Performance Optimization

The app is optimized for large-scale scanning:

- **Connection pooling**: Reuses HTTP connections for faster requests
- **Adaptive batching**: Automatically adjusts request rate
- **Retry logic**: Auto-retries failed requests (3 attempts)
- **Memory management**: Auto-cleanup of old results after 24 hours
- **Thread safety**: Proper locking for concurrent operations
- **Gunicorn workers**: 4 workers × 2 threads = handles 8 concurrent scans

## Scaling Recommendations

| Daily Scans | Instance Type | Estimated Cost |
|-------------|---------------|----------------|
| < 1,000     | Free          | $0/month       |
| 1K - 10K    | Starter       | $7/month       |
| 10K - 50K   | Standard      | $25/month      |
| 50K+        | Pro           | $85/month      |

## Troubleshooting

**App sleeping on Free tier?**
- Free instances spin down after 15 min of inactivity
- Upgrade to Starter ($7/mo) for always-on service

**Timeout errors?**
- Increase timeout in Procfile: `--timeout 180`
- Or upgrade instance for better performance

**Memory errors?**
- Upgrade to Standard (2GB RAM)
- Results auto-expire after 24h to prevent buildup

**Slow scanning?**
- Network latency is the main bottleneck
- Consider upgrading to Pro for better CPU/network

## Monitoring

- **Health check**: `https://your-app.onrender.com/health`
- **Render logs**: Real-time logs in dashboard
- **Metrics**: Render provides CPU, memory, and request metrics

## Future Enhancements

Planned for v2.0:
- User authentication
- Database storage (PostgreSQL)
- API endpoints
- Webhooks for completed scans
- Schedule recurring scans
- Email notifications

## Support

For issues or questions, check the Render logs first:
1. Go to your service in Render
2. Click "Logs"
3. Look for error messages

## License

MIT License - free for personal and commercial use
