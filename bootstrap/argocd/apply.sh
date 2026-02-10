#!/bin/sh

kubectl kustomize --enable-helm --load-restrictor=LoadRestrictionsNone --enable-alpha-plugins . \
    | kubectl -n argocd apply -f -

kubectl -n argocd wait --timeout=60s --for condition=Established \
        crd/applications.argoproj.io \
        crd/applicationsets.argoproj.io
