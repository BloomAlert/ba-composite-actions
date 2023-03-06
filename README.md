# BloomAlert Composite Actions 

This repo contains the following structure:

```
├── test-python             # Steps to Lint with flake8, test with pytest and upload coverage to Codecov
│   └──  action.yml          
├── build-podman            # Build with Podman (to generate docker v2 image format)
│   └──  action.yml          
├── build-docker            # Build with docker buildx (with cache) 
│   └──  action.yml         
├── deploy-cloudrun         # Deploy to Cloud Run 
│   └──  action.yml          
```

To create more composite actions, create a folder with a descriptive name, 
and inside add `action.yml` file with the composite action. 
