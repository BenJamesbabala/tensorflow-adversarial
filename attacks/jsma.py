import tensorflow as tf


def jsma(model, x, y, nb_epoch=None, tol=1.0, eps=1.0, clip_min=0.0,
         clip_max=1.0, pair=False, min_proba=0.0):
    xshape = tf.shape(x)
    n = xshape[0]
    target = tf.cond(tf.equal(0, tf.rank(y)),
                     lambda: tf.zeros([n], dtype=tf.int32)+y,
                     lambda: y)

    if pair:
        _jsma_fn = _jsma2_impl
    else:
        _jsma_fn = _jsma_impl

    def _fn(i):
        # `xi` is of the shape (1, ....), the first dimension is the
        # number of samples, 1 in this case.  `yi` is just a scalar,
        # denoting the target class index.
        xi = tf.gather(x, [i])
        yi = tf.gather(target, i)

        # `xadv` is of the shape (1, ...), same as xi.
        xadv = _jsma_fn(model, xi, yi, nb_epoch=nb_epoch, tol=tol,
                        eps=eps, clip_min=clip_min, clip_max=clip_max,
                        min_proba=min_proba)
        return xadv[0]

    return tf.map_fn(_fn, tf.range(n), dtype=tf.float32,
                     back_prop=False, name='jsma_batch')


def _jsma_impl(model, xi, yi, nb_epoch=None, tol=1., eps=1.,
               clip_min=0., clip_max=1., min_proba=0.):

    if nb_epoch is None:
        n = tf.to_float(tf.size(xi)) * tol
        nb_epoch = tf.to_int32(tf.floor(n))

    def _cond(x_adv, epoch, pixel_mask):
        ybar = tf.reshape(model(x_adv), [-1])
        proba = ybar[yi]
        label = tf.to_int32(tf.argmax(ybar, axis=0))
        return tf.reduce_all([tf.less(epoch, nb_epoch),
                              tf.reduce_any(pixel_mask),
                              tf.logical_or(tf.not_equal(yi, label),
                                            tf.less(proba, min_proba))],
                             name='_jsma_step_cond')

    def _body(x_adv, epoch, pixel_mask):
        ybar = model(x_adv)

        y_target = tf.slice(ybar, [0, yi], [-1, 1])
        dy_dx, = tf.gradients(ybar, x_adv)

        dt_dx, = tf.gradients(y_target, x_adv)
        do_dx = tf.subtract(dy_dx, dt_dx)
        score = tf.multiply(dt_dx, tf.abs(do_dx))

        pixel_domain = tf.logical_and(
            pixel_mask, tf.logical_and(dt_dx>=0, do_dx<=0))

        # ensure that pixel_domain is not empty
        pixel_domain = tf.cond(tf.reduce_any(pixel_domain),
                               lambda: pixel_domain,
                               lambda: tf.logical_and(pixel_mask,
                                                      dt_dx>=0))

        ind = tf.where(pixel_domain)
        score = tf.gather_nd(score, ind)

        p = tf.argmax(score, axis=0)
        p = tf.gather(ind, p)
        p = tf.expand_dims(p, axis=0)
        p = tf.to_int32(p)
        dx = tf.scatter_nd(p, [eps], tf.shape(x_adv), name='dx')

        x_adv = tf.stop_gradient(x_adv+dx)
        x_adv = tf.clip_by_value(x_adv, clip_min, clip_max)

        epoch += 1
        pixel_mask = tf.cond(tf.greater(eps, 0),
                             lambda: tf.less(x_adv, clip_max),
                             lambda: tf.greater(x_adv, clip_min))

        return x_adv, epoch, pixel_mask

    epoch = tf.Variable(0, tf.int32)
    x_adv = tf.identity(xi)
    pixel_mask = tf.cond(tf.greater(eps, 0),
                         lambda: tf.less(xi, clip_max),
                         lambda: tf.greater(xi, clip_min))

    x_adv, _, _ = tf.while_loop(_cond, _body,
                                (x_adv, epoch, pixel_mask),
                                back_prop=False, name='jsma_step')

    return x_adv


def _jsma2_impl(model, xi, yi, nb_epoch=None, tol=1.0, eps=1.0,
                clip_min=0.0, clip_max=1.0, min_proba=0.0):

    if nb_epoch is None:
        n = tf.to_float(tf.size(xi)) * tol * 0.5
        nb_epoch = tf.to_int32(tf.floor(n))

    def _cond(x_adv, epoch, pixel_mask):
        ybar = tf.reshape(model(x_adv), [-1])
        proba = ybar[yi]
        label = tf.to_int32(tf.argmax(ybar, axis=0))
        return tf.reduce_all([tf.less(epoch, nb_epoch),
                              tf.reduce_any(pixel_mask),
                              tf.logical_or(tf.not_equal(yi, label),
                                            tf.less(proba, min_proba))],
                             name='_jsma2_step_cond')

    def _body(x_adv, epoch, pixel_mask):
        ybar = model(x_adv)

        y_target = tf.slice(ybar, [0, yi], [-1, 1])
        dy_dx, = tf.gradients(ybar, x_adv)

        dt_dx, = tf.gradients(y_target, x_adv)
        do_dx = dy_dx - dt_dx

        ind = tf.where(pixel_mask)
        n = tf.shape(ind)
        n = n[0]

        ind2 = tf.range(n)
        batch_size = tf.constant(100)

        def _maxpair_batch_cond(i0, j0, v0, start):
            return tf.less(start, n)

        def _maxpair_batch_body(i0, j0, v0, start):
            count = tf.reduce_min([batch_size, n-start])
            ind3 = tf.slice(ind2, [start], [count])

            # Selection C(n, 2), e.g., if n=4, a=[0 0 1 0 1 2], b=[1 2
            # 2 3 3 3], the corresponding element in each array makes
            # a pair, i.e., the pair index are store separately.  A
            # special case is when there is only one pixel left.
            a, b = tf.meshgrid(ind3, ind3)
            c = tf.cond(tf.greater(count, 1),
                        lambda: tf.less(a, b),
                        lambda: tf.less_equal(a, b))
            c = tf.where(c)
            a, b = tf.gather_nd(a, c), tf.gather_nd(b, c)

            # ii, jj contains indices to pixels
            ii, jj = tf.gather(ind, a), tf.gather(ind, b)

            ti, oi = tf.gather_nd(dt_dx, ii), tf.gather_nd(do_dx, ii)
            tj, oj = tf.gather_nd(dt_dx, jj), tf.gather_nd(do_dx, jj)

            # the gradient of each pair is the sum of individuals
            t, o = ti+tj, oi+oj

            # increase target probability while decrease others
            c = tf.logical_and(t>=0, o<=0)

            # ensure that c is not empty
            c = tf.cond(tf.reduce_any(c), lambda: c, lambda: t>=0)

            # ensure that c is not empty
            c = tf.cond(tf.reduce_any(c), lambda: c,
                        lambda: tf.ones_like(c, dtype=bool))
            c = tf.where(c)

            t, o = tf.gather_nd(t, c), tf.gather_nd(o, c)
            ii, jj = tf.gather_nd(ii, c), tf.gather_nd(jj, c)

            # saliency score
            score = tf.multiply(t, tf.abs(o))

            # find the max pair in current batch
            p = tf.argmax(score, axis=0)
            v = tf.reduce_max(score, axis=0)
            i, j = tf.gather(ii, p), tf.gather(jj, p)
            i, j = tf.to_int32(i), tf.to_int32(j)

            i1, j1, v1 = tf.cond(tf.greater(v, v0),
                                 lambda: (i, j, v),
                                 lambda: (i0, j0, v0))
            return i1, j1, v1, start+batch_size

        i = tf.to_int32(tf.gather(ind, 0))
        j = tf.to_int32(tf.gather(ind, 1))
        v = tf.Variable(-1.)
        start = tf.Variable(0)

        # Find max saliency pair in batch.  Naive iteration through
        # the pair takes O(n^2).  Vectorized implementation may
        # speedup the running time significantly, at the expense of
        # O(n^2) space.  So Instead we find the max pair with batch
        # max, during each batch we use vectorized implementation.
        i, j, _, _ = tf.while_loop(_maxpair_batch_cond,
                                   _maxpair_batch_body,
                                   (i, j, v, start), back_prop=False)

        dx = tf.scatter_nd([i], [eps], tf.shape(x_adv)) +\
             tf.scatter_nd([j], [eps], tf.shape(x_adv))

        x_adv = tf.stop_gradient(x_adv+dx)
        x_adv = tf.clip_by_value(x_adv, clip_min, clip_max)
        epoch += 1
        pixel_mask = tf.cond(tf.greater(eps, 0),
                             lambda: tf.less(x_adv, clip_max),
                             lambda: tf.greater(x_adv, clip_min))

        return x_adv, epoch, pixel_mask

    epoch = tf.Variable(0, tf.int32)
    x_adv = tf.identity(xi)
    pixel_mask = tf.cond(tf.greater(eps, 0),
                         lambda: tf.less(xi, clip_max),
                         lambda: tf.greater(xi, clip_min))
    x_adv, _, _ = tf.while_loop(_cond, _body, (xi, epoch, pixel_mask),
                                back_prop=False, name='jsma2_step')
    return x_adv
