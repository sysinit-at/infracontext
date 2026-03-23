# Infracontext tasks

# Deploy landing page to infracontext.net
deploy-site:
    rsync -av --delete site/index.html wke@s.proxy-25:/var/www/infracontext/
