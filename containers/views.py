# Copyright Evan Hazlett and contributors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.utils.translation import ugettext as _
from django.contrib import messages
from django.core.urlresolvers import reverse
from django.http import HttpResponse
from django.db.models import Q
from django.utils.html import strip_tags
from django.core import serializers
from django.shortcuts import render_to_response
from containers.models import Container
from hosts.models import Host
from metrics.models import Metric
from django.template import RequestContext
from containers.forms import (CreateContainerForm,
    ImportRepositoryForm, ImageBuildForm)
from shipyard import utils
from docker import client
import urllib
import random
import json
import tempfile
import shlex

def handle_upload(f):
    tmp_file = tempfile.mktemp()
    with open(tmp_file, 'w') as d:
        for c in f.chunks():
            d.write(c)
    return tmp_file

@login_required
def index(request):
    hosts = Host.objects.filter(enabled=True)
    show_all = True if request.GET.has_key('showall') else False
    containers = Container.objects.filter(host__in=hosts, is_running=True).\
            order_by('description')
    ctx = {
        'hosts': hosts,
        'containers': containers,
        'show_all': show_all,
    }
    return render_to_response('containers/index.html', ctx,
        context_instance=RequestContext(request))

@login_required
def container_details(request, container_id=None):
    c = Container.objects.get(container_id=container_id)
    metrics = Metric.objects.filter(source=c.container_id).order_by('-timestamp')
    cpu_metrics = metrics.filter(counter='cpu')[:30]
    mem_metrics = metrics.filter(counter='memory')[:30]
    # build custom data to use unix timestamp instead of python datetime
    cpu_data = []
    mem_data = []
    for m in cpu_metrics:
        cpu_data.append({
            'counter': m.counter,
            'value': m.value,
            'timestamp': m.unix_timestamp(),
            })
    for m in mem_metrics:
        mem_data.append({
            'counter': m.counter,
            'value': m.value,
            'timestamp': m.unix_timestamp(),
            })
    ctx = {
        'container': c,
        'cpu_metrics': json.dumps(cpu_data),
        'mem_metrics': json.dumps(mem_data),
    }
    return render_to_response('containers/container_details.html', ctx,
        context_instance=RequestContext(request))

@login_required
def create_container(request):
    form = CreateContainerForm()
    if request.method == 'POST':
        # save
        form = CreateContainerForm(request.POST)
        if form.is_valid():
            image = form.data.get('image')
            name = form.data.get('name')
            hostname = form.data.get('hostname')
            description = form.data.get('description')
            environment = form.data.get('environment')
            command = form.data.get('command')
            memory = form.data.get('memory', 0)
            links = form.data.get('links', None)
            volume = form.data.get('volume')
            volumes_from = form.data.get('volumes_from')
            ports = form.data.get('ports', '').split()
            hosts = form.data.getlist('hosts')
            private = form.data.get('private')
            privileged = form.data.get('privileged')
            user = None
            status = False
            for i in hosts:
                host = Host.objects.get(id=i)
                if private:
                    user = request.user
                try:
                    c_id, status = host.create_container(image, command, ports,
                        environment=environment, memory=memory,
                        description=description, volumes=volume,
                        volumes_from=volumes_from, privileged=privileged,
                        links=links, name=name, owner=user,
                        hostname=hostname)
                    messages.add_message(request, messages.INFO, _('Created') + ' {0}'.format(
                        image))
                except Exception, e:
                    print(e)
                    messages.error(request, e)
                    status = False
            if not hosts:
                messages.add_message(request, messages.ERROR, _('No hosts selected'))
            return redirect(reverse('containers.views.index'))
    ctx = {
        'form_create_container': form,
    }
    return render_to_response('containers/create_container.html', ctx,
        context_instance=RequestContext(request))

@login_required
def container_info(request, container_id=None):
    '''
    Gets / Sets container metatdata

    '''
    if request.method == 'POST':
        data = request.POST
        container_id = data.get('container-id')
        c = Container.objects.get(container_id=container_id)
        c.description = data.get('description')
        c.save()
        return redirect(reverse('containers.views.container_details',
            args=(c.container_id,)))
    c = Container.objects.get(container_id=container_id)
    data = serializers.serialize('json', [c], ensure_ascii=False)[1:-1]
    return HttpResponse(data, content_type='application/json')

@login_required
def container_logs(request, host, container_id):
    '''
    Gets the specified container logs

    '''
    h = Host.objects.get(name=host)
    c = Container.objects.get(container_id=container_id)
    logs = h.get_container_logs(container_id).strip()
    # format
    if logs:
        logs = utils.convert_ansi_to_html(logs)
    else:
        logs = None
    ctx = {
        'container': c,
        'logs': logs
    }
    return render_to_response('containers/container_logs.html', ctx,
        context_instance=RequestContext(request))

@login_required
def restart_container(request, host, container_id):
    h = Host.objects.get(name=host)
    h.restart_container(container_id)
    messages.add_message(request, messages.INFO, _('Restarted') + ' {0}'.format(
        container_id))
    return redirect('containers.views.index')

@login_required
def stop_container(request, host, container_id):
    h = Host.objects.get(name=host)
    try:
        h.stop_container(container_id)
        messages.add_message(request, messages.INFO, _('Stopped') + ' {0}'.format(
            container_id))
    except Exception, e:
        messages.add_message(request, messages.ERROR, e)
    return redirect('containers.views.index')

@login_required
def destroy_container(request, host, container_id):
    h = Host.objects.get(name=host)
    try:
        h.destroy_container(container_id)
        messages.add_message(request, messages.INFO, _('Removed') + ' {0}'.format(
            container_id))
    except Exception, e:
        messages.add_message(request, messages.ERROR, e)
    return redirect('containers.views.index')

@login_required
def attach_container(request, host, container_id):
    h = Host.objects.get(name=host)
    c = Container.objects.get(container_id=container_id)
    session_id = utils.generate_console_session(h, c)
    ctx = {
        'container_id': container_id,
        'container_name': c.description or container_id,
        'ws_url': 'ws://{0}/console/{1}/'.format(request.META['HTTP_HOST'], session_id),
    }
    return render_to_response("containers/attach.html", ctx,
        context_instance=RequestContext(request))

@login_required
def clone_container(request, host, container_id):
    h = Host.objects.get(name=host)
    try:
        h.clone_container(container_id)
        messages.add_message(request, messages.INFO, _('Cloned') + ' {0}'.format(
            container_id))
    except Exception, e:
        messages.add_message(request, messages.ERROR, e)
    return redirect('containers.views.index')

@login_required
def refresh(request):
    '''
    Invalidates host cache and redirects to container view

    '''
    for h in Host.objects.filter(enabled=True):
        h.invalidate_cache()
    return redirect('containers.views.index')

@require_http_methods(['GET'])
@login_required
def search_repository(request):
    '''
    Searches the docker index for repositories

    :param query: Query to search for

    '''
    query = request.GET.get('query', {})
    # get random host for query -- just needs a connection
    hosts = Host.objects.filter(enabled=True)
    rnd = random.randint(0, len(hosts)-1)
    host = hosts[rnd]
    url = 'http://{0}:{1}'.format(host.hostname, host.port)
    c = client.Client(url)
    data = c.search(query)
    return HttpResponse(json.dumps(data), content_type='application/json')

@require_http_methods(['POST'])
@login_required
def build_image(request):
    '''
    Builds a container image

    '''
    form = ImageBuildForm(request.POST)
    url = form.data.get('url')
    tag = form.data.get('tag')
    hosts = form.data.getlist('hosts')
    # dockerfile takes precedence
    docker_file = None
    if request.FILES.has_key('dockerfile'):
        docker_file = handle_upload(request.FILES.get('dockerfile'))
    else:
        docker_file = tempfile.mktemp()
        urllib.urlretrieve(url, docker_file)
    for i in hosts:
        host = Host.objects.get(id=i)
        args = (docker_file, tag)
        # TODO: update to celery
        #utils.get_queue('shipyard').enqueue(host.build_image, args=args,
        #    timeout=3600)
    messages.add_message(request, messages.INFO,
        _('Building image from docker file.  This may take a few minutes.'))
    return redirect(reverse('index'))

@csrf_exempt
@require_http_methods(['POST'])
@login_required
def toggle_protect_container(request, host_id, container_id):
    enabled = request.POST.get('enabled')
    host = Host.objects.get(id=host_id)
    container = Container.objects.get(host=host, container_id=container_id)
    if enabled == 'true':
        container.protected = True
    else:
        container.protected = False
    container.save()
    return HttpResponse('done')

